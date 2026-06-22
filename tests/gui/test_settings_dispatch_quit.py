"""Regression tests for ``SettingsWindow._dispatch_quit``.

The tray-quit ".NET Framework / stopped working" dialog was a pythonnet crash
(``System.NullReferenceException`` in ``TypeManager.AllocateTypeObject``) while
marshalling a .NET exception thrown by pywebview's WinForms/WebView2 FormClosed
teardown back into the finalizing interpreter. That crash sits *below* the
Python ``try/except`` and WinForms ``ThreadException`` swallow layers, so the
only fix is to not run the teardown at all: on Windows ``_dispatch_quit`` now
hard-exits via ``os._exit(0)`` before any .NET call. macOS keeps the graceful
``safe_quit_window`` path (no .NET; NSWindow close doesn't recurse).

We call the unbound method with a stub ``self`` so we don't have to construct a
real ``SettingsWindow`` (Bridge / Config / pywebview window).
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock


def _call_dispatch_quit(monkeypatch, platform: str):
    """Invoke ``_dispatch_quit`` under ``platform``; return (exit_calls, safe_quit_calls, fake_self)."""
    monkeypatch.setattr(sys, "platform", platform)

    exit_calls: list = []
    monkeypatch.setattr(os, "_exit", lambda code=0: exit_calls.append(code))

    from sayzo_agent.gui.settings import window as win_mod

    safe_quit_calls: list = []
    monkeypatch.setattr(win_mod, "safe_quit_window", lambda w: safe_quit_calls.append(w))

    fake_self = types.SimpleNamespace(_quitting=False)
    fake_window = MagicMock()
    win_mod.SettingsWindow._dispatch_quit(fake_self, fake_window)
    return exit_calls, safe_quit_calls, fake_self, fake_window


def test_dispatch_quit_hard_exits_on_windows(monkeypatch):
    """Windows: hard-exit via os._exit(0); the crashing teardown is skipped."""
    exit_calls, safe_quit_calls, fake_self, _ = _call_dispatch_quit(monkeypatch, "win32")

    assert fake_self._quitting is True
    assert exit_calls == [0], "Windows quit must hard-exit via os._exit(0)"
    assert safe_quit_calls == [], (
        "safe_quit_window must NOT run on Windows — it triggers the WinForms "
        "FormClosed teardown that crashes pythonnet's exception marshaller"
    )


def test_dispatch_quit_uses_safe_quit_on_macos(monkeypatch):
    """macOS / other: keep the graceful safe_quit_window path, no hard-exit."""
    exit_calls, safe_quit_calls, fake_self, fake_window = _call_dispatch_quit(
        monkeypatch, "darwin"
    )

    assert fake_self._quitting is True
    assert exit_calls == [], "macOS must not hard-exit — no pythonnet teardown crash there"
    assert safe_quit_calls == [fake_window]
