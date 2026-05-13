"""Unit tests for the win_shutdown helper.

Same stubbing pattern as test_safe_quit.py: we don't depend on real
pythonnet — install fake ``Microsoft.Win32`` + ``System.Threading`` +
``System.Windows.Forms`` modules in sys.modules and import the helper
fresh per test.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _no_real_hard_exit_timer(monkeypatch):
    """Defang ``_arm_hard_exit_timer`` for the whole module.

    Several tests fire ``SessionEnding``, which arms a real ``threading.Timer``
    that calls ``os._exit(0)`` 2 s later. If a test runs slower than that
    (or pytest doesn't tear down fast enough between tests), the timer
    fires mid-suite and kills the pytest process. Mock the Timer class
    so it stores the call but never actually fires. Tests that need to
    inspect the timer arming behavior install their own stub on top.
    """
    import threading as _threading

    class _NoopTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(_threading, "Timer", _NoopTimer)


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
            "System.Threading",
            "Microsoft",
            "Microsoft.Win32",
            "sayzo_agent.gui.common.win_shutdown",
            "sayzo_agent.gui.common.safe_quit",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _install_stubs(uid: str = "master"):
    """Install fake .NET modules that the helper imports lazily.

    Returns a SimpleNamespace exposing the mocks tests assert on:
      - system_events: Microsoft.Win32.SystemEvents (so we can read its
        SessionEnding-added handler back and fire it manually).
      - thread_exception: System.Windows.Forms.Application.ThreadException
        (same — captures the registered handler).
      - exit_thread: Application.ExitThread (called via safe_quit_window).
      - set_mode: Application.SetUnhandledExceptionMode.
    """
    # webview stubs for safe_quit_window.
    fake_browser_form = MagicMock()
    fake_browser_form.IsDisposed = False
    fake_winforms_module = types.ModuleType("webview.platforms.winforms")
    fake_winforms_module.BrowserView = types.SimpleNamespace(
        instances={uid: fake_browser_form}
    )
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    sys.modules.setdefault("webview.platforms", types.ModuleType("webview.platforms"))
    sys.modules["webview.platforms.winforms"] = fake_winforms_module

    # System.Windows.Forms — Application + ThreadException + UnhandledExceptionMode.
    exit_thread_mock = MagicMock(name="Application.ExitThread")
    thread_exception_event = _FakeEvent("ThreadException")
    set_mode_mock = MagicMock(name="SetUnhandledExceptionMode")
    fake_app = types.SimpleNamespace(
        ExitThread=exit_thread_mock,
        ThreadException=thread_exception_event,
        SetUnhandledExceptionMode=set_mode_mock,
    )
    fake_unhandled_mode = types.SimpleNamespace(CatchException="CatchException")
    fake_system_windows_forms = types.ModuleType("System.Windows.Forms")
    fake_system_windows_forms.Application = fake_app
    fake_system_windows_forms.UnhandledExceptionMode = fake_unhandled_mode
    sys.modules["System.Windows.Forms"] = fake_system_windows_forms
    sys.modules.setdefault("System.Windows", types.ModuleType("System.Windows"))

    # System + System.Threading.
    fake_action = MagicMock(name="Action")
    fake_system_module = types.ModuleType("System")
    fake_system_module.Action = fake_action
    sys.modules["System"] = fake_system_module

    fake_threading_module = types.ModuleType("System.Threading")
    fake_threading_module.ThreadExceptionEventHandler = lambda fn: fn
    sys.modules["System.Threading"] = fake_threading_module

    # Microsoft.Win32 — SystemEvents.SessionEnding event.
    session_ending_event = _FakeEvent("SessionEnding")
    fake_system_events = types.SimpleNamespace(SessionEnding=session_ending_event)
    fake_microsoft_win32_module = types.ModuleType("Microsoft.Win32")
    fake_microsoft_win32_module.SystemEvents = fake_system_events
    fake_microsoft_win32_module.SessionEndingEventHandler = lambda fn: fn
    sys.modules.setdefault("Microsoft", types.ModuleType("Microsoft"))
    sys.modules["Microsoft.Win32"] = fake_microsoft_win32_module

    return types.SimpleNamespace(
        browser_form=fake_browser_form,
        session_events=session_ending_event,
        thread_events=thread_exception_event,
        exit_thread=exit_thread_mock,
        set_mode=set_mode_mock,
    )


class _FakeEvent:
    """Capture handlers added via ``+=`` so tests can fire them manually.

    Pythonnet exposes .NET events with ``+=`` / ``-=`` operators that
    forward to ``add_<Event>`` / ``remove_<Event>``. We mirror just the
    add side with ``__iadd__``.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, sender=None, args=None) -> None:
        for h in list(self.handlers):
            h(sender, args)


def test_no_op_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    # Must not raise even with no stubs in place — install bails before
    # touching any .NET module.
    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)


def test_windows_installs_both_handlers(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)

    # Both handlers registered.
    assert len(stubs.session_events.handlers) == 1, "SessionEnding not subscribed"
    assert len(stubs.thread_events.handlers) == 1, "ThreadException not subscribed"
    # Mode switched so .NET routes unhandled exceptions to our handler
    # instead of the JIT debugger. threadScope=False is load-bearing —
    # pywebview hosts the UI on a separate STA thread, so threadScope=True
    # would leave that thread on the default mode and the safety net would
    # silently miss every crash (the v2.7.11–v2.14.1 regression).
    stubs.set_mode.assert_called_once()
    _mode_arg, thread_scope_arg = stubs.set_mode.call_args.args
    assert thread_scope_arg is False, (
        "SetUnhandledExceptionMode threadScope must be False so the mode "
        "applies to pywebview's STA thread, not just Python main"
    )


def test_session_ending_calls_safe_quit(monkeypatch):
    """SessionEnding firing → ExitThread invoked on UI thread (via safe_quit)."""
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)

    # Simulate Windows sending WM_QUERYENDSESSION.
    args = types.SimpleNamespace(Reason="SystemShutdown")
    stubs.session_events.fire(sender=None, args=args)

    # safe_quit_window's Windows path posts ExitThread via BrowserForm.BeginInvoke.
    stubs.browser_form.BeginInvoke.assert_called_once()
    # destroy() must NOT run — that's the path that would crash if WebView2
    # is already dying.
    window.destroy.assert_not_called()


def test_session_ending_flips_set_quitting_callback(monkeypatch):
    """Idle Settings: SessionEnding must mark _quitting=True so on_closing
    doesn't try to hide() during shutdown."""
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    callback_called = []
    install_shutdown_protection(
        window, set_quitting=lambda: callback_called.append(True)
    )

    stubs.session_events.fire(sender=None, args=types.SimpleNamespace(Reason="Logoff"))

    assert callback_called == [True], "set_quitting callback not invoked"


def test_thread_exception_swallows_pywebview_teardown(monkeypatch, caplog):
    """A FormClosed-handler exception during shutdown gets logged + swallowed."""
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)

    # Simulate the exact pythonnet → FormClosed → MarshaledInvoke trace
    # the user reported. Python looks up __str__ on the type, not the
    # instance, so we need a real class (SimpleNamespace won't work).
    class _PywebviewTeardownException:
        def __str__(self) -> str:
            return (
                "System.ArgumentException: Process with an Id of 9888 is not running.\n"
                "   at System.Windows.Forms.Control.MarshaledInvoke(...)\n"
                "   at __System_Windows_Forms_FormClosedEventHandlerDispatcher.Invoke(...)\n"
                "   at System.Windows.Forms.Form.OnFormClosed(...)\n"
                "   at System.Windows.Forms.Form.WmClose(...)\n"
            )

    args = types.SimpleNamespace(Exception=_PywebviewTeardownException())

    with caplog.at_level("WARNING", logger="sayzo_agent.gui.common.win_shutdown"):
        # Must not raise.
        stubs.thread_events.fire(sender=None, args=args)

    assert any(
        "swallowed pywebview teardown exception" in rec.getMessage()
        for rec in caplog.records
    ), "expected swallow log line"


def test_thread_exception_passes_through_real_bugs(monkeypatch, caplog):
    """A genuine app exception (no pywebview signatures) is NOT silently
    swallowed — we log it at ERROR so it still surfaces."""
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)

    class _RealBugException:
        def __str__(self) -> str:
            return (
                "System.InvalidOperationException: widget frobnicated wrong\n"
                "   at MyApp.Widget.Frobnicate(...)\n"
            )

    args = types.SimpleNamespace(Exception=_RealBugException())

    with caplog.at_level("ERROR", logger="sayzo_agent.gui.common.win_shutdown"):
        stubs.thread_events.fire(sender=None, args=args)

    assert any(
        "not swallowed" in rec.getMessage()
        for rec in caplog.records
    ), "expected pass-through log for non-pywebview exception"


def test_session_ending_arms_hard_exit_timer(monkeypatch):
    """SessionEnding must arm the os._exit fallback timer.

    The user-visible contract: Windows shutdown is never blocked waiting on
    a pywebview internal hang. If safe_quit_window's WM_QUIT post doesn't
    drain the message loop in time, ``_HARD_EXIT_TIMEOUT_SECS`` later we
    call os._exit unconditionally so the process gets out of Windows'
    way. Test by mocking threading.Timer — we don't actually want
    os._exit to fire during the test suite.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    import threading as _threading

    timer_args = []

    class _StubTimer:
        def __init__(self, interval, function):
            timer_args.append((interval, function))
            self.daemon = False

        def start(self):
            timer_args.append("started")

    monkeypatch.setattr(_threading, "Timer", _StubTimer)

    from sayzo_agent.gui.common.win_shutdown import (
        _HARD_EXIT_TIMEOUT_SECS,
        install_shutdown_protection,
    )

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)
    stubs.session_events.fire(sender=None, args=types.SimpleNamespace(Reason="SystemShutdown"))

    # Timer constructed with the documented delay and start() called.
    interval, fire_fn = timer_args[0]
    assert interval == _HARD_EXIT_TIMEOUT_SECS
    assert timer_args[1] == "started"
    assert callable(fire_fn)


def test_session_ending_subscription_failure_does_not_propagate(monkeypatch):
    """If SystemEvents import / subscribe fails, install must return cleanly.
    The agent works without shutdown protection; it just crashes at shutdown
    the way it did before this fix."""
    monkeypatch.setattr(sys, "platform", "win32")
    _install_stubs()

    # Break Microsoft.Win32 import.
    sys.modules["Microsoft.Win32"] = None  # type: ignore[assignment]

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    # Must not raise.
    install_shutdown_protection(window)
