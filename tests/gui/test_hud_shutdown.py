"""Unit tests for the v2.16.0 HUD shutdown hardening.

Covers four surfaces:

* ``sayzo_agent.gui.hud.shutdown_hooks`` — Qt-level commitDataRequest /
  aboutToQuit / hard-exit timer.
* ``sayzo_agent.gui.common.win_shutdown.install_session_ending_callback`` —
  generic agent-side SystemEvents subscription.
* ``sayzo_agent.gui.common.mac_shutdown.observe_will_power_off`` —
  NSWorkspaceWillPowerOff observer.
* ``sayzo_agent.gui.hud.launcher.HudLauncher.quit_sync`` — sync wrapper
  around the async ``quit()`` for non-asyncio callers.

We stub PySide6 / AppKit / pythonnet modules in ``sys.modules`` so the
tests don't need real Qt / .NET / Cocoa.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Module isolation fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_modules():
    """Snapshot + restore the modules we touch so tests don't bleed."""
    saved = {
        k: sys.modules.get(k)
        for k in (
            "PySide6",
            "PySide6.QtCore",
            "PySide6.QtGui",
            "PySide6.QtWebEngineCore",
            "PySide6.QtWebEngineWidgets",
            "PySide6.QtWidgets",
            "AppKit",
            "Foundation",
            "Microsoft",
            "Microsoft.Win32",
            "sayzo_agent.gui.hud.shutdown_hooks",
            "sayzo_agent.gui.common.mac_shutdown",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


@pytest.fixture(autouse=True)
def _no_real_hard_exit_timer(monkeypatch):
    """Defang the hard-exit timer so it can't os._exit() out of pytest."""
    import threading as _threading

    class _NoopTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(_threading, "Timer", _NoopTimer)


# ---------------------------------------------------------------------------
# PySide6 stubs for shutdown_hooks
# ---------------------------------------------------------------------------


class _FakeSignal:
    """Minimal Qt-signal shim for tests."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot, connection_type=None):
        self.slots.append((slot, connection_type))

    def fire(self, *args, **kwargs):
        for slot, _ct in self.slots:
            slot(*args, **kwargs)


def _install_pyside6_stub():
    """Install fake PySide6 modules sufficient for ``install_qt_shutdown_hooks``."""

    # PySide6.QtCore — Qt enum + QTimer.singleShot.
    qt_core = types.ModuleType("PySide6.QtCore")

    class _ConnectionType:
        DirectConnection = "DirectConnection"
        AutoConnection = "AutoConnection"

    class _Qt:
        ConnectionType = _ConnectionType

    qt_core.Qt = _Qt

    qtimer_singleshots: list = []

    class _QTimer:
        @staticmethod
        def singleShot(delay_ms, callable_):
            qtimer_singleshots.append((delay_ms, callable_))

    qt_core.QTimer = _QTimer
    sys.modules["PySide6.QtCore"] = qt_core

    # PySide6.QtGui — QGuiApplication module marker only; import succeeds.
    qt_gui = types.ModuleType("PySide6.QtGui")
    qt_gui.QGuiApplication = MagicMock(name="QGuiApplication")
    sys.modules["PySide6.QtGui"] = qt_gui

    # PySide6.QtWebEngineCore — QWebEngineProfile.defaultProfile().clearHttpCache.
    qt_webengine_core = types.ModuleType("PySide6.QtWebEngineCore")
    default_profile_mock = MagicMock(name="default_profile")
    profile_cls = MagicMock(name="QWebEngineProfile")
    profile_cls.defaultProfile = MagicMock(return_value=default_profile_mock)
    qt_webengine_core.QWebEngineProfile = profile_cls
    sys.modules["PySide6.QtWebEngineCore"] = qt_webengine_core

    sys.modules.setdefault("PySide6", types.ModuleType("PySide6"))

    return types.SimpleNamespace(
        singleshots=qtimer_singleshots,
        default_profile=default_profile_mock,
    )


def _make_fake_app() -> MagicMock:
    """A minimal ``app`` substitute exposing the two signals."""
    app = MagicMock(name="QApplication")
    app.commitDataRequest = _FakeSignal()
    app.aboutToQuit = _FakeSignal()
    app.quit = MagicMock(name="app.quit")
    return app


# ---------------------------------------------------------------------------
# shutdown_hooks tests
# ---------------------------------------------------------------------------


def test_install_qt_shutdown_hooks_connects_both_signals():
    stubs = _install_pyside6_stub()

    from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

    app = _make_fake_app()
    view = MagicMock(name="view")
    install_qt_shutdown_hooks(app, view_provider=lambda: view)

    assert len(app.commitDataRequest.slots) == 1
    assert len(app.aboutToQuit.slots) == 1
    # commitDataRequest connection MUST use DirectConnection — otherwise the
    # slot runs on the wrong thread and the OS doesn't get a synchronous ack.
    _slot, conn_type = app.commitDataRequest.slots[0]
    assert conn_type == "DirectConnection"


def test_commit_data_request_arms_hard_exit_and_queues_app_quit(monkeypatch):
    """Firing the signal must (1) arm the hard-exit Timer (2) queue app.quit."""
    stubs = _install_pyside6_stub()

    armed_timers: list = []
    import threading as _threading

    real_timer = _threading.Timer

    class _CapturingTimer:
        def __init__(self, interval, function):
            armed_timers.append((interval, function))

        def start(self):
            pass

    monkeypatch.setattr(_threading, "Timer", _CapturingTimer)

    from sayzo_agent.gui.hud.shutdown_hooks import (
        _HUD_HARD_EXIT_TIMEOUT_SECS,
        install_qt_shutdown_hooks,
    )

    app = _make_fake_app()
    install_qt_shutdown_hooks(app, view_provider=lambda: None)

    session_manager = MagicMock()
    # commitDataRequest fires with a QSessionManager argument.
    app.commitDataRequest.fire(session_manager)

    # Hard-exit timer was armed with the documented delay.
    assert len(armed_timers) == 1
    assert armed_timers[0][0] == _HUD_HARD_EXIT_TIMEOUT_SECS
    # QTimer.singleShot(0, app.quit) was queued.
    assert len(stubs.singleshots) == 1
    delay_ms, callable_ = stubs.singleshots[0]
    assert delay_ms == 0
    assert callable_ is app.quit


def test_about_to_quit_handler_tears_down_webengine_view():
    stubs = _install_pyside6_stub()

    from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

    app = _make_fake_app()
    view = MagicMock(name="view")
    install_qt_shutdown_hooks(app, view_provider=lambda: view)

    # Fire aboutToQuit.
    app.aboutToQuit.fire()

    # Page severed, view marked for deferred deletion.
    view.setPage.assert_called_once_with(None)
    view.deleteLater.assert_called_once()
    # Default profile flushed to disk.
    stubs.default_profile.clearHttpCache.assert_called_once()


def test_about_to_quit_survives_view_provider_returning_none():
    stubs = _install_pyside6_stub()

    from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

    app = _make_fake_app()
    install_qt_shutdown_hooks(app, view_provider=lambda: None)

    # Must not raise even though view is None.
    app.aboutToQuit.fire()

    # Profile flush still runs (it doesn't depend on view).
    stubs.default_profile.clearHttpCache.assert_called_once()


def test_about_to_quit_swallows_teardown_errors():
    stubs = _install_pyside6_stub()

    from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

    app = _make_fake_app()
    view = MagicMock(name="view")
    view.setPage.side_effect = RuntimeError("boom")
    install_qt_shutdown_hooks(app, view_provider=lambda: view)

    # Must not raise — aboutToQuit handlers can't fail or shutdown chains downstream
    # of them never run.
    app.aboutToQuit.fire()


def test_install_skips_cleanly_if_pyside_missing():
    # Force PySide6.QtCore import failure.
    sys.modules["PySide6.QtCore"] = None  # type: ignore[assignment]

    from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

    app = _make_fake_app()
    # Must not raise.
    install_qt_shutdown_hooks(app, view_provider=lambda: None)
    # No signal connections happened.
    assert len(app.commitDataRequest.slots) == 0
    assert len(app.aboutToQuit.slots) == 0


# ---------------------------------------------------------------------------
# install_session_ending_callback tests
# ---------------------------------------------------------------------------


def _install_systemevents_stub():
    """Stub ``Microsoft.Win32.SystemEvents``."""

    class _Event:
        def __init__(self) -> None:
            self.handlers: list = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def fire(self, sender=None, args=None):
            for h in list(self.handlers):
                h(sender, args)

    session_ending = _Event()
    fake_system_events = types.SimpleNamespace(SessionEnding=session_ending)
    fake_module = types.ModuleType("Microsoft.Win32")
    fake_module.SystemEvents = fake_system_events
    fake_module.SessionEndingEventHandler = lambda fn: fn
    sys.modules.setdefault("Microsoft", types.ModuleType("Microsoft"))
    sys.modules["Microsoft.Win32"] = fake_module
    return session_ending


def test_install_session_ending_callback_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.win_shutdown import (
        install_session_ending_callback,
    )

    assert install_session_ending_callback(lambda: None) is False


def test_install_session_ending_callback_subscribes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    events = _install_systemevents_stub()

    from sayzo_agent.gui.common.win_shutdown import (
        install_session_ending_callback,
    )

    fired: list = []
    assert install_session_ending_callback(lambda: fired.append("ok")) is True
    assert len(events.handlers) == 1

    # Firing the .NET event should call our Python callback.
    events.fire(sender=None, args=types.SimpleNamespace(Reason="SystemShutdown"))
    assert fired == ["ok"]


def test_install_session_ending_callback_swallows_callback_errors(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    events = _install_systemevents_stub()

    from sayzo_agent.gui.common.win_shutdown import (
        install_session_ending_callback,
    )

    def _raises():
        raise RuntimeError("agent shutdown handler exploded")

    install_session_ending_callback(_raises)
    # Must not propagate — pythonnet would rethrow as a CLR exception inside
    # the SystemEvents dispatcher otherwise.
    events.fire(sender=None, args=types.SimpleNamespace(Reason="Logoff"))


# ---------------------------------------------------------------------------
# mac_shutdown.observe_will_power_off tests
# ---------------------------------------------------------------------------


def test_observe_will_power_off_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    # Make sure no state lingers between tests in case macOS test ran first.
    import sayzo_agent.gui.common.mac_shutdown as mac_shutdown

    monkeypatch.setattr(mac_shutdown, "_observer_handle", None)

    assert mac_shutdown.observe_will_power_off(lambda: None) is False


def test_observe_will_power_off_pyobjc_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    # Pretend AppKit can't be imported.
    sys.modules["AppKit"] = None  # type: ignore[assignment]

    import sayzo_agent.gui.common.mac_shutdown as mac_shutdown

    monkeypatch.setattr(mac_shutdown, "_observer_handle", None)

    assert mac_shutdown.observe_will_power_off(lambda: None) is False


# ---------------------------------------------------------------------------
# HudLauncher.quit_sync tests
# ---------------------------------------------------------------------------


def test_quit_sync_noop_when_loop_not_running():
    from sayzo_agent.gui.hud.launcher import HudLauncher

    launcher = HudLauncher()
    # _loop is None until start() runs. Must not raise.
    launcher.quit_sync(timeout_secs=0.1)


def test_quit_sync_marshals_onto_running_loop():
    """quit_sync schedules the async quit() coroutine on the launcher's loop."""
    from sayzo_agent.gui.hud.launcher import HudLauncher

    launcher = HudLauncher()
    called = []

    async def _fake_quit(timeout_secs=3.0):
        called.append(timeout_secs)

    # Replace the real quit with a fast stub.
    launcher.quit = _fake_quit  # type: ignore[assignment]

    loop = asyncio.new_event_loop()
    launcher._loop = loop

    def _run_loop():
        loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    try:
        launcher.quit_sync(timeout_secs=0.5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=1.0)
        loop.close()

    assert called == [0.5]
