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
def _no_real_os_exit(monkeypatch):
    """Defang ``os._exit`` for the whole module.

    The SessionEnding handler now **hard-exits** via ``os._exit(0)`` instead of
    draining the WinForms message loop (it skips the pywebview FormClosed
    teardown that crashes pythonnet's exception marshaller). Several tests fire
    ``SessionEnding``; without this fixture that real ``os._exit(0)`` would kill
    the pytest process. Replace it with a recorder so the handler completes
    normally and tests can assert it was called. Tests that want to inspect the
    exit request the fixture by name to read the recorded codes.
    """
    import os as _os

    calls: list = []
    monkeypatch.setattr(_os, "_exit", lambda code=0: calls.append(code))
    return calls


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
      - exit_thread / browser_form: the safe_quit_window path stubs. As of
        v3.20.3 the SessionEnding handler hard-exits (os._exit) instead of
        calling safe_quit_window, so these are retained to assert that path
        is NOT taken (test_session_ending_hard_exits asserts BeginInvoke
        is never called).
      - set_mode: Application.SetUnhandledExceptionMode.
    """
    # webview stubs: kept so we can assert the safe_quit_window/ExitThread
    # path is NOT exercised on SessionEnding (it hard-exits instead).
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


def test_session_ending_hard_exits(monkeypatch, _no_real_os_exit):
    """SessionEnding firing → os._exit(0), NOT safe_quit / BeginInvoke.

    safe_quit_window would post WM_QUIT and let the WinForms FormClosed teardown
    run, which throws a .NET exception pythonnet crashes while marshalling
    (System.NullReferenceException in TypeManager.AllocateTypeObject → the
    WerFault ".NET Framework" dialog). That crash is below the Python/
    ThreadException swallow layers, so the only fix is to skip the teardown by
    hard-exiting before any .NET teardown call runs.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    stubs = _install_stubs()

    from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection

    window = MagicMock()
    window.uid = "master"
    install_shutdown_protection(window)

    # Simulate Windows sending WM_QUERYENDSESSION.
    args = types.SimpleNamespace(Reason="SystemShutdown")
    stubs.session_events.fire(sender=None, args=args)

    # Hard-exit fired...
    assert _no_real_os_exit == [0], "SessionEnding must hard-exit via os._exit(0)"
    # ...and the crashing teardown path must NOT run.
    stubs.browser_form.BeginInvoke.assert_not_called()
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
