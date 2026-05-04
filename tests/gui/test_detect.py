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
    _PERMISSIONS_MARKER_NAME,
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


def _write_onboarded_marker(cfg: Config) -> None:
    (cfg.data_dir / _PERMISSIONS_MARKER_NAME).touch()


# ---------------------------------------------------------------------------
# Basic signals (platform-agnostic)
# ---------------------------------------------------------------------------


def test_empty_state_is_incomplete(cfg: Config) -> None:
    status = detect_setup(cfg)
    assert status.has_token is False
    assert status.is_complete is False


def test_token_only_is_incomplete(cfg: Config) -> None:
    _write_token(cfg)
    status = detect_setup(cfg)
    assert status.has_token is True
    # Onboarded marker is still required.
    assert status.is_complete is False


def test_token_plus_onboarded_is_complete_when_not_darwin(cfg: Config) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.has_token is True
    assert status.has_mic_permission is None
    # Windows now walks the shortcut + notifications screens too, so the
    # onboarded marker is the same gate it is on macOS.
    assert status.has_permissions_onboarded is True
    assert status.is_complete is True


# ---------------------------------------------------------------------------
# macOS mic permission probe (opt-in via probe_mac_permission=True)
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
    probe: bool = True,
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
        return detect_setup(cfg, probe_mac_permission=probe)


def test_mac_permission_granted(cfg: Config, mac_env: Path) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    status = _run_darwin_detect(cfg, returncode=0, audio_tap_path=mac_env)
    assert status.has_mic_permission is True
    assert status.has_permissions_onboarded is True
    assert status.is_complete is True


def test_mac_permission_denied(cfg: Config, mac_env: Path) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    status = _run_darwin_detect(
        cfg,
        returncode=_MAC_EXIT_PERMISSION_DENIED,
        audio_tap_path=mac_env,
    )
    assert status.has_mic_permission is False
    # Explicit deny still blocks is_complete on darwin.
    assert status.is_complete is False


def test_mac_permission_timeout_means_granted(cfg: Config, mac_env: Path) -> None:
    """If audio-tap is still running past the probe timeout it cleared the
    permission gate — treat as granted."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    status = _run_darwin_detect(cfg, timeout=True, audio_tap_path=mac_env)
    assert status.has_mic_permission is True
    assert status.is_complete is True


def test_mac_binary_missing_is_unknown(cfg: Config) -> None:
    """If audio-tap can't be located the probe is inconclusive — don't block."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    status = _run_darwin_detect(cfg, audio_tap_found=False)
    assert status.has_mic_permission is None
    assert status.is_complete is True


def test_mac_unknown_exit_code_is_unknown(cfg: Config, mac_env: Path) -> None:
    """Non-zero, non-77 exit code is treated as inconclusive rather than
    denied — prevents false-deny wedging the GUI."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    status = _run_darwin_detect(cfg, returncode=13, audio_tap_path=mac_env)
    assert status.has_mic_permission is None
    assert status.is_complete is True


def test_probe_is_off_by_default(cfg: Config) -> None:
    """Default probe=False is the new behaviour: no audio-tap spawn, no TCC
    dialog before the GUI has shown an explanation."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "darwin"), patch(
        "sayzo_agent.gui.setup.detect.subprocess.run"
    ) as fake_run:
        status = detect_setup(cfg)
    assert fake_run.called is False
    assert status.has_mic_permission is None
    # Onboarded + no explicit deny → complete.
    assert status.is_complete is True


# ---------------------------------------------------------------------------
# macOS permissions-onboarded marker
# ---------------------------------------------------------------------------


def test_mac_not_onboarded_blocks_is_complete(cfg: Config) -> None:
    """Without the .permissions_onboarded_v1 marker, is_complete must be
    False on darwin so the service opens the GUI and shows the Permissions
    screen on next launch."""
    _write_token(cfg)
    # Intentionally no marker.
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "darwin"):
        status = detect_setup(cfg)
    assert status.has_permissions_onboarded is False
    assert status.is_complete is False


def test_mac_onboarded_marker_flows_into_is_complete(cfg: Config) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "darwin"):
        status = detect_setup(cfg)
    assert status.has_permissions_onboarded is True
    assert status.is_complete is True


def test_not_onboarded_blocks_is_complete_on_non_darwin(cfg: Config) -> None:
    """Windows now walks the same linear setup flow (shortcut + notifications
    screens), so the onboarded marker gates is_complete on both platforms."""
    _write_token(cfg)
    # Don't write the marker.
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.has_permissions_onboarded is False
    assert status.is_complete is False


# ---------------------------------------------------------------------------
# SetupStatus.to_dict
# ---------------------------------------------------------------------------


def test_status_to_dict_includes_all_fields() -> None:
    s = SetupStatus(
        has_token=True,
        has_mic_permission=None,
        has_permissions_onboarded=True,
        account_state="ok",
        is_complete=True,
    )
    assert s.to_dict() == {
        "has_token": True,
        "has_mic_permission": None,
        "has_permissions_onboarded": True,
        "account_state": "ok",
        "is_complete": True,
    }


# ---------------------------------------------------------------------------
# Account-state gating (web-onboarding requirement)
# ---------------------------------------------------------------------------


def _write_account_cache(cfg: Config, account_state: str) -> None:
    from sayzo_agent.account.cache import (
        CACHE_FILENAME,
        CACHE_SCHEMA_VERSION,
    )

    payload = {
        "version": CACHE_SCHEMA_VERSION,
        "account_state": account_state,
        "onboarding_complete": account_state == "ok",
        "onboarding_url": "https://sayzo.app/onboarding",
        "email": "user@example.com",
        "user_id": "usr_x",
        "fetched_at": "2026-05-04T12:00:00+00:00",
    }
    (cfg.data_dir / CACHE_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_account_state_unknown_does_not_block(cfg: Config) -> None:
    """No cache yet → ``unknown`` → setup-complete proceeds. The arm gate
    falls back to allow when the cache is missing, so this matches."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.account_state == "unknown"
    assert status.is_complete is True


def test_account_state_ok_passes(cfg: Config) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    _write_account_cache(cfg, "ok")
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.account_state == "ok"
    assert status.is_complete is True


def test_account_state_onboarding_required_blocks(cfg: Config) -> None:
    """Cache says the user hasn't finished web onboarding → setup is not
    complete, so the GUI re-opens at FinishSignup on next launch."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    _write_account_cache(cfg, "onboarding_required")
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.account_state == "onboarding_required"
    assert status.is_complete is False


def test_account_state_suspended_blocks(cfg: Config) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    _write_account_cache(cfg, "suspended")
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.is_complete is False


def test_account_state_deleted_blocks(cfg: Config) -> None:
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    _write_account_cache(cfg, "deleted")
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.is_complete is False


def test_kill_switch_disables_account_gate(cfg: Config) -> None:
    """SAYZO_AUTH__ACCOUNT_CHECK_ENABLED=0 → account_state is reported but
    not factored into is_complete. Lets us roll back the gate without
    shipping a new agent if the endpoint goes sideways."""
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    _write_account_cache(cfg, "onboarding_required")
    cfg.auth.account_check_enabled = False
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.account_state == "onboarding_required"
    assert status.is_complete is True


def test_corrupt_cache_treated_as_unknown(cfg: Config) -> None:
    """Garbled JSON in the cache file should NOT lock the user out — the
    detector treats it as ``unknown`` and the boot refresh repopulates."""
    from sayzo_agent.account.cache import CACHE_FILENAME

    (cfg.data_dir / CACHE_FILENAME).write_text("not json", encoding="utf-8")
    _write_token(cfg)
    _write_onboarded_marker(cfg)
    with patch("sayzo_agent.gui.setup.detect.sys.platform", "win32"):
        status = detect_setup(cfg)
    assert status.account_state == "unknown"
    assert status.is_complete is True
