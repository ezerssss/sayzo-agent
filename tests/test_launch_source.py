"""Smoke tests for the user-click vs. auto-start heuristic.

Exhaustive parent-process matrix lives in the ``service()`` integration —
this is just enough to pin the contract: the function exists, returns a
bool, doesn't raise on any platform, and the macOS branch correctly
classifies launchd-spawned processes via ``XPC_SERVICE_NAME``.
"""
from __future__ import annotations

import sys

import pytest

from sayzo_agent import launch_source


def test_looks_user_launched_returns_bool() -> None:
    # Whatever the test runner's parent is, the function must not raise
    # and must return a bool. Real values depend on how pytest was
    # invoked (cmd / pwsh / IDE), so we don't assert True/False.
    result = launch_source.looks_user_launched()
    assert isinstance(result, bool)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS heuristic only")
def test_mac_launchd_autostart_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # LaunchAgent-spawned: bundle id matches AND XPC_SERVICE_NAME is the
    # plist Label. Treat as auto-start, no Settings.
    monkeypatch.setenv("__CFBundleIdentifier", "com.sayzo.agent")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.sayzo.agent")
    assert launch_source._looks_user_launched_mac() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS heuristic only")
def test_mac_launchservices_click_is_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LaunchServices-spawned (Finder click, Spotlight, Dock click while
    # not running): bundle id matches but XPC_SERVICE_NAME is unset.
    monkeypatch.setenv("__CFBundleIdentifier", "com.sayzo.agent")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert launch_source._looks_user_launched_mac() is True


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS heuristic only")
def test_mac_dev_run_outside_bundle_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dev: ``python -m sayzo_agent service`` from Terminal — not running
    # inside the .app bundle, so __CFBundleIdentifier is unset (or set
    # to Terminal's bundle, which is not com.sayzo.agent). Treat as
    # silent so iterative dev doesn't auto-pop Settings every time.
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert launch_source._looks_user_launched_mac() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS heuristic only")
def test_mac_other_bundle_id_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some other bundle launched us (unlikely but possible if a wrapper
    # invokes our binary). Don't claim user-launched from inside a
    # different host's bundle.
    monkeypatch.setenv("__CFBundleIdentifier", "com.apple.Terminal")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert launch_source._looks_user_launched_mac() is False
