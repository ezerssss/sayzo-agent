"""Unit tests for the safe_quit_window helper.

We don't depend on a real pywebview / pythonnet install — the function
imports ``webview.platforms.winforms``, ``System.Windows.Forms``, and
``System`` lazily inside its body, so we stub those modules in
``sys.modules`` before calling.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


def _install_winforms_stubs(uid: str = "master", *, is_disposed: bool = False):
    """Stub ``webview.platforms.winforms`` + ``System.Windows.Forms`` + ``System``.

    Returns ``(mock_browser_form, exit_thread_mock)`` so tests can assert
    on what got invoked.
    """
    fake_browser_form = MagicMock()
    fake_browser_form.IsDisposed = is_disposed

    fake_winforms = types.ModuleType("webview.platforms.winforms")
    fake_winforms.BrowserView = types.SimpleNamespace(instances={uid: fake_browser_form})

    exit_thread_mock = MagicMock(name="Application.ExitThread")
    fake_app = types.SimpleNamespace(ExitThread=exit_thread_mock)
    fake_system_winforms = types.SimpleNamespace(Application=fake_app)

    fake_action = MagicMock(name="Action")

    sys.modules.setdefault("webview", types.ModuleType("webview"))
    sys.modules.setdefault("webview.platforms", types.ModuleType("webview.platforms"))
    sys.modules["webview.platforms.winforms"] = fake_winforms

    fake_system_module = types.ModuleType("System")
    fake_system_module.Action = fake_action
    sys.modules["System"] = fake_system_module

    fake_system_windows_forms = types.ModuleType("System.Windows.Forms")
    fake_system_windows_forms.Application = fake_app
    sys.modules["System.Windows.Forms"] = fake_system_windows_forms
    sys.modules.setdefault(
        "System.Windows", types.ModuleType("System.Windows"),
    )
    return fake_browser_form, exit_thread_mock, fake_action


@pytest.fixture(autouse=True)
def _isolate_modules():
    saved = {
        k: sys.modules.get(k)
        for k in (
            "webview",
            "webview.platforms",
            "webview.platforms.winforms",
            "System",
            "System.Windows",
            "System.Windows.Forms",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_windows_path_calls_begininvoke_exit_thread(monkeypatch):
    """Happy path: Windows + alive form → BeginInvoke ExitThread on UI thread."""
    monkeypatch.setattr(sys, "platform", "win32")
    bv, _, _ = _install_winforms_stubs()

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    safe_quit_window(window)

    bv.BeginInvoke.assert_called_once()
    # destroy() must NOT be called — we're using ExitThread instead so
    # FormClosed never fires.
    window.destroy.assert_not_called()


def test_falls_back_to_destroy_on_non_windows(monkeypatch):
    """macOS / Linux: keep using window.destroy()."""
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    safe_quit_window(window)

    window.destroy.assert_called_once()


def test_falls_back_to_destroy_when_instance_missing(monkeypatch):
    """Defensive: pywebview already cleaned up → fall back to destroy().

    Real-world: if the form was already destroyed by some other path,
    BrowserView.instances doesn't have our uid. Don't try to BeginInvoke
    on a None reference.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    _install_winforms_stubs(uid="other_uid")  # 'master' won't be in instances

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    safe_quit_window(window)

    # Fell through to destroy()
    window.destroy.assert_called_once()


def test_falls_back_to_destroy_on_disposed_form(monkeypatch):
    """Defensive: form is already disposed → BeginInvoke would throw.

    Forms can transition to IsDisposed=True between the dict lookup and
    our Invoke call. Detect and fall back so we don't propagate an
    ObjectDisposedException out of _dispatch_quit.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    bv, _, _ = _install_winforms_stubs(is_disposed=True)

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    safe_quit_window(window)

    bv.BeginInvoke.assert_not_called()
    window.destroy.assert_called_once()


def test_falls_back_when_begininvoke_raises(monkeypatch):
    """Defensive: BeginInvoke can raise (e.g., handle gone). Fall back."""
    monkeypatch.setattr(sys, "platform", "win32")
    bv, _, _ = _install_winforms_stubs()
    bv.BeginInvoke.side_effect = Exception("handle gone")

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    safe_quit_window(window)

    # ExitThread bypass failed → fall back to destroy().
    window.destroy.assert_called_once()


def test_destroy_failure_is_swallowed(monkeypatch):
    """Last-resort destroy() failure shouldn't crash the caller.

    _dispatch_quit's ``return`` after safe_quit_window depends on the
    helper not raising — otherwise the stdin reader thread leaks the
    exception and the EOF fallback can re-call _dispatch_quit.
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    window = MagicMock()
    window.uid = "master"
    window.destroy.side_effect = RuntimeError("boom")

    # Must not raise.
    safe_quit_window(window)
