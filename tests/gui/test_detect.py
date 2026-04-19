"""Unit tests for sayzo_agent.gui.setup.detect.

Runs on any platform — the macOS audio-tap probe is mocked via
``subprocess.run``. No real network, no real audio.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sayzo_agent.config import Config
from sayzo_agent.gui.setup.detect import (
    _MAC_EXIT_PERMISSION_DENIED,
    SetupStatus,
    detect_setup,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _write_token(cfg: Config) -> None:
    cfg.auth_path.write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": 9_999_999_999,
            }
        ),
        encoding="utf-8",
    )


def _write_model(cfg: Config, *, size: int = 1024) -> None:
    path = cfg.models_dir / cfg.llm.filename
    path.write_bytes(b"\x00" * size)


# ---------------------------------------------------------------------------
# Basic signals (platform-agnostic)
# ---------------------------------------------------------------------------


def test_empty_state_is_incomplete(cfg: Config) -> None:
    status = detect_setup(cfg, probe_mac_permission=False)
    assert status.has_token is False
    assert status.has_model is False
    assert status.is_complete is False


def test_token_only_is_incomplete(cfg: Config) -> None:
    _write_token(cfg)
    status = detect_setup(cfg, probe_mac_permission=False)
    assert status.has_token is True
    assert status.has_model is False
    assert status.is_complete is False


def test_model_only_is_incomplete(cfg: Config) -> None:
    _write_model(cfg)
    status = detect_setup(cfg, probe_mac_permission=False)
    assert status.has_token is False
    assert status.has_model is True
    assert status.is_complete is False


def test_empty_model_file_is_incomplete(cfg: Config) -> None:
    """Zero-byte model file (e.g. half-written download) must fail has_model."""
    _write_token(cfg)
    _write_model(cfg, size=0)
    status = detect_setup(cfg, probe_mac_permission=False)
    assert status.has_model is False
    assert status.is_complete is False


def test_both_signals_complete_when_not_darwin(cfg: Config) -> None:
    _write_token(cfg)
    _write_model(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.has_token is True
    assert status.has_model is True
    assert status.has_mic_permission is None
    assert status.is_complete is True


# ---------------------------------------------------------------------------
# macOS mic permission probe
# ---------------------------------------------------------------------------


@pytest.fixture
def mac_env(tmp_path: Path) -> Path:
    """Drop a fake audio-tap binary into the expected location."""
    binary = tmp_path / "audio-tap"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _run_darwin_detect(
    cfg: Config,
    *,
    returncode: int | None = 0,
    timeout: bool = False,
    audio_tap_found: bool = True,
    audio_tap_path: Path | None = None,
):
    def fake_run(cmd, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1.5))
        result = subprocess.CompletedProcess(cmd, returncode or 0, b"", b"")
        return result

    def fake_find() -> str:
        if not audio_tap_found:
            raise FileNotFoundError("audio-tap not installed")
        return str(audio_tap_path) if audio_tap_path else "/fake/audio-tap"

    with patch("sayzo_agent.gui.setup.detect.sys.platform", "darwin"), patch(
        "sayzo_agent.gui.setup.detect.subprocess.run", side_effect=fake_run
    ), patch("sayzo_agent.capture.system_mac._find_audio_tap", side_effect=fake_find):
        return detect_setup(cfg)


def test_mac_permission_granted(cfg: Config, mac_env: Path) -> None:
    _write_token(cfg)
    _write_model(cfg)
    status = _run_darwin_detect(
        cfg, returncode=0, audio_tap_path=mac_env
    )
    assert status.has_mic_permission is True
    assert status.is_complete is True


def test_mac_permission_denied(cfg: Config, mac_env: Path) -> None:
    _write_token(cfg)
    _write_model(cfg)
    status = _run_darwin_detect(
        cfg,
        returncode=_MAC_EXIT_PERMISSION_DENIED,
        audio_tap_path=mac_env,
    )
    assert status.has_mic_permission is False
    assert status.is_complete is False


def test_mac_permission_timeout_means_granted(cfg: Config, mac_env: Path) -> None:
    """If audio-tap is still running past the probe timeout it cleared the
    permission gate — treat as granted."""
    _write_token(cfg)
    _write_model(cfg)
    status = _run_darwin_detect(
        cfg, timeout=True, audio_tap_path=mac_env
    )
    assert status.has_mic_permission is True
    assert status.is_complete is True


def test_mac_binary_missing_is_unknown(cfg: Config) -> None:
    """If audio-tap can't be located the probe is inconclusive — don't block."""
    _write_token(cfg)
    _write_model(cfg)
    status = _run_darwin_detect(cfg, audio_tap_found=False)
    assert status.has_mic_permission is None
    assert status.is_complete is True


def test_mac_unknown_exit_code_is_unknown(cfg: Config, mac_env: Path) -> None:
    """Non-zero, non-77 exit code is treated as inconclusive rather than
    denied — prevents false-deny wedging the GUI."""
    _write_token(cfg)
    _write_model(cfg)
    status = _run_darwin_detect(cfg, returncode=13, audio_tap_path=mac_env)
    assert status.has_mic_permission is None
    assert status.is_complete is True


def test_probe_can_be_skipped(cfg: Config) -> None:
    _write_token(cfg)
    _write_model(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "darwin"):
        status = detect_setup(cfg, probe_mac_permission=False)
    assert status.has_mic_permission is None
    # is_complete is True because None doesn't block on darwin.
    assert status.is_complete is True


# ---------------------------------------------------------------------------
# SetupStatus.to_dict
# ---------------------------------------------------------------------------


def test_status_to_dict_includes_is_complete() -> None:
    s = SetupStatus(
        has_token=True, has_model=True, has_mic_permission=None, is_complete=True
    )
    d = s.to_dict()
    assert d == {
        "has_token": True,
        "has_model": True,
        "has_mic_permission": None,
        "is_complete": True,
    }
