"""Tests for the platform-specific apply helpers.

These functions normally call ``os._exit(0)`` after spawning the
installer/swap-helper subprocess. The tests monkeypatch ``os._exit`` to raise
a sentinel exception so the test runner survives; the rest of the assertion
is on the recorded ``Popen`` invocation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sayzo_agent.update_stage import StagedUpdate
from sayzo_agent import update_apply_win
from sayzo_agent import update_apply_mac


class _ExitSentinel(Exception):
    """Raised by patched ``os._exit`` so the test process survives."""


def _patch_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(code: int) -> None:
        raise _ExitSentinel(code)

    monkeypatch.setattr(os, "_exit", boom)


class _PopenRecorder:
    """Stand-in for ``subprocess.Popen`` that captures the call args."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        # Return something Popen-like with a poll() that says "still running"
        # — the apply helpers don't read it, but be safe.
        class _FakePopen:
            pid = 99999

            def poll(self):
                return None

        return _FakePopen()


def _staged(tmp_path: Path, *, version: str = "0.2.0") -> StagedUpdate:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x")
    return StagedUpdate(
        version=version,
        platform="windows",
        sha256="deadbeef",
        notes="Release notes for v0.2.0.",
        payload_path=payload,
        ready_at="2026-05-12T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# update_apply_win
# ---------------------------------------------------------------------------


def test_win_spawn_passes_silent_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_exit(monkeypatch)
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)

    staged = _staged(tmp_path)
    with pytest.raises(_ExitSentinel):
        update_apply_win.spawn_installer_and_exit(staged)

    assert len(rec.calls) == 1
    args, kwargs = rec.calls[0]
    # First positional arg is the command list.
    cmd = args[0]
    assert cmd == [str(staged.payload_path), "/S"]


def test_win_spawn_uses_detached_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_exit(monkeypatch)
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)

    staged = _staged(tmp_path)
    with pytest.raises(_ExitSentinel):
        update_apply_win.spawn_installer_and_exit(staged)

    _, kwargs = rec.calls[0]
    # 0x00000008 | 0x00000200 | 0x08000000 — guard against an accidental flag
    # drop that would re-attach the installer console to the agent's group.
    assert kwargs["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert kwargs["creationflags"] & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert kwargs["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


# ---------------------------------------------------------------------------
# update_apply_mac
# ---------------------------------------------------------------------------


def test_mac_locate_helper_finds_dev_path() -> None:
    # The dev fallback in _locate_helper points at the repo's
    # installer/macos/apply_update.sh. With the script committed, this should
    # always resolve (sanity check on the locator).
    located = update_apply_mac._locate_helper()
    assert located is not None
    assert located.name == update_apply_mac.HELPER_NAME
    assert located.exists()


def test_mac_locate_app_bundle_returns_none_in_dev() -> None:
    # Running tests from a source tree, sys.executable is python(.exe), not
    # inside a .app — so the locator must refuse rather than half-execute the
    # apply path.
    assert update_apply_mac._locate_app_bundle() is None


def test_mac_spawn_raises_when_app_bundle_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Dev environment: helper resolves but app bundle doesn't -> RuntimeError.
    _patch_exit(monkeypatch)
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)

    staged = _staged(tmp_path)
    with pytest.raises(RuntimeError, match="Sayzo.app bundle"):
        update_apply_mac.spawn_swap_helper_and_exit(staged)
    # Popen must NOT have been called — early exit before spawn.
    assert rec.calls == []


def test_mac_spawn_passes_dmg_and_app_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Stub the locators so the test passes regardless of host OS / layout.
    fake_helper = tmp_path / "apply_update.sh"
    fake_helper.write_text("#!/bin/bash\n", encoding="utf-8")
    fake_app = tmp_path / "Sayzo.app"
    fake_app.mkdir()

    monkeypatch.setattr(update_apply_mac, "_locate_helper", lambda: fake_helper)
    monkeypatch.setattr(update_apply_mac, "_locate_app_bundle", lambda: fake_app)
    _patch_exit(monkeypatch)
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)

    staged = _staged(tmp_path)
    with pytest.raises(_ExitSentinel):
        update_apply_mac.spawn_swap_helper_and_exit(staged)

    assert len(rec.calls) == 1
    args, kwargs = rec.calls[0]
    cmd = args[0]
    assert cmd == [
        "/bin/bash",
        str(fake_helper),
        str(staged.payload_path),
        str(fake_app),
    ]
    # start_new_session=True is critical: without setsid, the spawned helper
    # would still be in the agent's process group and get SIGHUP when the
    # agent exits, then die without completing the swap.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_mac_spawn_raises_when_helper_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(update_apply_mac, "_locate_helper", lambda: None)
    monkeypatch.setattr(update_apply_mac, "_locate_app_bundle", lambda: tmp_path)
    _patch_exit(monkeypatch)
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)

    staged = _staged(tmp_path)
    with pytest.raises(RuntimeError, match="apply_update.sh"):
        update_apply_mac.spawn_swap_helper_and_exit(staged)
    assert rec.calls == []
