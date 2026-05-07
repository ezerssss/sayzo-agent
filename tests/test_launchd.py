"""Smoke tests for sayzo_agent.gui.setup.launchd (SMAppService wrapper).

The full SMAppService.register codepath only runs on macOS inside a frozen
.app bundle and requires pyobjc-framework-ServiceManagement. These tests
cover the cross-platform contract — no-op on Windows / Linux, legacy-plist
cleanup helper exercises file I/O without touching launchd, and the bundle
detection short-circuits cleanly when there is no .app to register.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sayzo_agent.gui.setup import launchd


def test_module_exports_remain_stable() -> None:
    # Public surface is what __main__.py and tray.py import — pin it so
    # a careless rewrite doesn't silently break callers at runtime.
    assert "ensure_launchd_registered" in dir(launchd)
    assert "is_registered" in dir(launchd)
    assert "unregister_login_item" in dir(launchd)
    assert launchd.LAUNCH_AGENT_LABEL == "com.sayzo.agent"
    assert launchd.LAUNCH_AGENT_PLIST_NAME == "com.sayzo.agent.plist"


@pytest.mark.skipif(sys.platform == "darwin", reason="non-darwin contract")
def test_ensure_launchd_registered_noop_on_non_darwin() -> None:
    # Windows / Linux dev runs hit this path. Must not raise, must not
    # try to import pyobjc, must just return False.
    assert launchd.ensure_launchd_registered() is False
    assert launchd.is_registered() is False
    assert launchd.unregister_login_item() is False


def test_ensure_launchd_registered_accepts_load_immediately_kwarg() -> None:
    # The kwarg is preserved for API back-compat (it was meaningful in
    # the pre-v2.7.0 signature). Calling with it must not raise.
    assert launchd.ensure_launchd_registered(load_immediately=True) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="legacy plist lives in macOS home")
def test_remove_legacy_plist_noop_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point HOME at an empty tmp dir so the legacy plist is guaranteed
    # absent. Cleanup helper must return False (nothing to remove) and
    # must not raise.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert launchd._remove_legacy_plist() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="legacy plist lives in macOS home")
def test_remove_legacy_plist_deletes_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simulate an upgrade-in-progress: the v2.6.x plist still exists in
    # ~/Library/LaunchAgents/ when v2.7.0 boots. The cleanup helper must
    # delete it (so launchd doesn't reload it at next login) and report
    # True.
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / "Library" / "LaunchAgents" / "com.sayzo.agent.plist"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("<plist/>\n", encoding="utf-8")
    assert launchd._remove_legacy_plist() is True
    assert not legacy.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="bundle-detection is darwin-only")
def test_ensure_launchd_registered_skips_dev_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # When sys.executable is not inside a .app bundle (i.e. a dev run via
    # `python -m sayzo_agent`), registration must be skipped — there is
    # no bundle plist to register and no legacy plist to clean up.
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert launchd.ensure_launchd_registered() is False
