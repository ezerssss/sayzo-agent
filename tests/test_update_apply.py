"""Tests for the platform-specific apply helpers.

These functions normally call ``os._exit(0)`` after spawning the
installer/swap-helper subprocess. The tests monkeypatch ``os._exit`` to raise
a sentinel exception so the test runner survives; the rest of the assertion
is on the recorded ``Popen`` invocation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sayzo_agent.update_stage import STAGED_DIR_NAME, StagedUpdate
from sayzo_agent import update_apply, update_apply_win
from sayzo_agent import update_apply_mac
from sayzo_agent.update_apply import (
    APPLY_ATTEMPTS_FILE,
    MAX_APPLY_ATTEMPTS,
    QUIT_APPLY_FLAG_NAME,
    apply_staged_if_newer,
    clear_apply_attempts,
    clear_quit_apply_intent,
    get_failed_apply_version,
    has_quit_apply_intent,
    set_quit_apply_intent,
)


class _ExitSentinel(BaseException):
    """Raised by patched ``os._exit`` so the test process survives.

    Inherits from BaseException (not Exception) because it represents process
    termination — same semantics as SystemExit / KeyboardInterrupt — and must
    NOT be swallowed by the ``except Exception:`` guard inside
    :func:`update_apply.apply_staged_if_newer`.
    """


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


# ---------------------------------------------------------------------------
# Quit-apply intent flag (v2.12+)
#
# These primitives gate the quit-time apply call sites so a plain tray Quit
# no longer auto-installs a staged update. Settings → Install update, the
# tray "Install Sayzo vX.Y.Z" menu item, and the HUD "Install now" toast
# button each write the flag before triggering the quit.
# ---------------------------------------------------------------------------


def test_set_quit_apply_intent_creates_flag(tmp_path: Path) -> None:
    assert not has_quit_apply_intent(tmp_path)
    set_quit_apply_intent(tmp_path)
    assert has_quit_apply_intent(tmp_path)
    assert (tmp_path / QUIT_APPLY_FLAG_NAME).is_file()


def test_set_quit_apply_intent_is_idempotent(tmp_path: Path) -> None:
    set_quit_apply_intent(tmp_path)
    set_quit_apply_intent(tmp_path)
    assert has_quit_apply_intent(tmp_path)


def test_set_quit_apply_intent_creates_missing_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "data"
    assert not target.exists()
    set_quit_apply_intent(target)
    assert has_quit_apply_intent(target)


def test_clear_quit_apply_intent_removes_flag(tmp_path: Path) -> None:
    set_quit_apply_intent(tmp_path)
    clear_quit_apply_intent(tmp_path)
    assert not has_quit_apply_intent(tmp_path)


def test_clear_quit_apply_intent_is_safe_when_missing(tmp_path: Path) -> None:
    # Must not raise — boot-time clear runs unconditionally before any
    # session has had a chance to write the flag.
    clear_quit_apply_intent(tmp_path)
    assert not has_quit_apply_intent(tmp_path)


def test_apply_staged_if_newer_noop_when_nothing_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No stage on disk → apply is a no-op regardless of platform helper.
    # Patch sys.platform-specific helpers to record any spawn — none expected.
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)
    apply_staged_if_newer(tmp_path, "1.0.0", where="quit")
    assert rec.calls == []


def test_apply_staged_if_newer_noop_when_stage_not_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage exists but matches running version → no platform spawn.
    monkeypatch.setattr(
        update_apply, "read_staged",
        lambda data_dir: StagedUpdate(
            version="1.0.0", platform="windows", sha256="x", notes="",
            payload_path=tmp_path / "payload.exe", ready_at="x",
        ),
    )
    rec = _PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec)
    apply_staged_if_newer(tmp_path, "1.0.0", where="quit")
    assert rec.calls == []


# ---------------------------------------------------------------------------
# Apply-attempt retry cap (boot-loop guard)
#
# A broken stage (DMG hash matched but mount/rsync failed; NSIS rejected by
# managed-endpoint policy) used to make the agent unbootable into its tray —
# every restart re-detected the staged version, re-spawned the helper, the
# helper failed the same way, and the user saw only "exiting agent for swap"
# in the agent log before the process vanished. The cap stops the loop and
# the boot path surfaces a "Sayzo update failed" toast on the next launch.
# ---------------------------------------------------------------------------


def _stub_staged(monkeypatch: pytest.MonkeyPatch, *, version: str = "9.9.9") -> None:
    """Make ``update_apply.read_staged`` always return a strictly-newer stage."""
    fake = StagedUpdate(
        version=version, platform="x", sha256="x", notes="",
        payload_path=Path("/dev/null"), ready_at="x",
    )
    monkeypatch.setattr(update_apply, "read_staged", lambda data_dir: fake)


def _stub_helpers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace both platform spawn helpers with a recorder. Returns a list
    that's appended to on each call, so tests can assert spawn count without
    caring which platform branch the dispatcher took.
    """
    spawned: list[str] = []

    def _fake_win(staged):
        spawned.append("win")
        raise _ExitSentinel(0)

    def _fake_mac(staged):
        spawned.append("mac")
        raise _ExitSentinel(0)

    monkeypatch.setattr(update_apply_win, "spawn_installer_and_exit", _fake_win)
    monkeypatch.setattr(update_apply_mac, "spawn_swap_helper_and_exit", _fake_mac)
    return spawned


def test_apply_attempt_counter_increments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_staged(monkeypatch)
    spawned = _stub_helpers(monkeypatch)
    # First two attempts should each spawn (and exit via sentinel).
    for _ in range(2):
        with pytest.raises(_ExitSentinel):
            apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert len(spawned) == 2

    payload = json.loads((tmp_path / STAGED_DIR_NAME / APPLY_ATTEMPTS_FILE).read_text())
    assert payload["attempts"] == 2
    assert payload["version"] == "9.9.9"


def test_apply_caps_at_max_attempts_and_clears_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_staged(monkeypatch)
    spawned = _stub_helpers(monkeypatch)
    cleared = []
    monkeypatch.setattr(
        update_apply, "clear_staged",
        lambda data_dir: cleared.append(data_dir),
    )

    # Burn through MAX_APPLY_ATTEMPTS spawns.
    for _ in range(MAX_APPLY_ATTEMPTS):
        with pytest.raises(_ExitSentinel):
            apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert len(spawned) == MAX_APPLY_ATTEMPTS
    assert cleared == []

    # The (MAX+1)th call must NOT spawn — the cap fires first, clearing the
    # stage and returning normally so the agent can keep booting.
    apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert len(spawned) == MAX_APPLY_ATTEMPTS  # unchanged
    assert cleared == [tmp_path]
    # Attempts file is preserved past the cap so the next boot can read it
    # via get_failed_apply_version().
    assert (tmp_path / STAGED_DIR_NAME / APPLY_ATTEMPTS_FILE).is_file()


def test_apply_attempt_resets_on_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _stub_helpers(monkeypatch)

    # Burn the cap on v9.9.9.
    _stub_staged(monkeypatch, version="9.9.9")
    monkeypatch.setattr(update_apply, "clear_staged", lambda data_dir: None)
    for _ in range(MAX_APPLY_ATTEMPTS):
        with pytest.raises(_ExitSentinel):
            apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    apply_staged_if_newer(tmp_path, "1.0.0", where="boot")  # cap fires
    assert len(spawned) == MAX_APPLY_ATTEMPTS

    # New version drops in (fresh download). Counter must reset, so the next
    # call spawns even though the previous version was capped.
    _stub_staged(monkeypatch, version="9.9.10")
    with pytest.raises(_ExitSentinel):
        apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert len(spawned) == MAX_APPLY_ATTEMPTS + 1


def test_get_failed_apply_version_after_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_staged(monkeypatch, version="9.9.9")
    _stub_helpers(monkeypatch)
    monkeypatch.setattr(update_apply, "clear_staged", lambda data_dir: None)

    # Below the cap → no failed version reported.
    for _ in range(MAX_APPLY_ATTEMPTS - 1):
        with pytest.raises(_ExitSentinel):
            apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert get_failed_apply_version(tmp_path) is None

    # Cross the cap (one more spawn brings us to MAX, the next call caps).
    with pytest.raises(_ExitSentinel):
        apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    apply_staged_if_newer(tmp_path, "1.0.0", where="boot")
    assert get_failed_apply_version(tmp_path) == "9.9.9"


def test_clear_apply_attempts_consumes_failure_marker(tmp_path: Path) -> None:
    target = tmp_path / STAGED_DIR_NAME / APPLY_ATTEMPTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": "9.9.9",
        "attempts": MAX_APPLY_ATTEMPTS,
        "first_attempt_at": "x",
        "last_attempt_at": "x",
    }))
    assert get_failed_apply_version(tmp_path) == "9.9.9"
    clear_apply_attempts(tmp_path)
    assert get_failed_apply_version(tmp_path) is None
    # Idempotent — second call must not raise.
    clear_apply_attempts(tmp_path)


def test_clear_apply_attempts_safe_when_missing(tmp_path: Path) -> None:
    clear_apply_attempts(tmp_path)
    assert get_failed_apply_version(tmp_path) is None
