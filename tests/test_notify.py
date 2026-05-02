"""Tests for the notifier wrapper + duration formatting.

The notifier spins up a dedicated asyncio loop on a daemon thread. Tests
patch ``desktop_notifier.DesktopNotifier`` with an async fake before
constructing ``sayzo_agent.notify.DesktopNotifier`` so no real OS toast
surface is hit.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types

from sayzo_agent.app import _format_duration
from sayzo_agent.notify import DesktopNotifier, NoopNotifier


def _install_fake_backend(monkeypatch, async_cls, button_cls=None) -> None:
    """Register a fake ``desktop_notifier`` package.

    ``async_cls`` is the class used for the async ``DesktopNotifier``; it
    must expose an async ``send`` method. ``button_cls`` replaces the
    ``Button`` dataclass — defaults to a minimal stand-in that captures
    the ``title`` and ``on_pressed`` callback."""
    root = types.ModuleType("desktop_notifier")
    root.Icon = type("Icon", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
    root.DesktopNotifier = async_cls  # type: ignore[attr-defined]

    if button_cls is None:
        class _Button:
            def __init__(self, *, title, on_pressed=None):
                self.title = title
                self.on_pressed = on_pressed
        button_cls = _Button
    root.Button = button_cls  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "desktop_notifier", root)


def test_format_duration_subminute():
    assert _format_duration(12.4) == "12s"
    assert _format_duration(59.4) == "59s"


def test_format_duration_minutes():
    assert _format_duration(60.0) == "1 min"
    assert _format_duration(90.0) == "2 min"  # rounds
    assert _format_duration(725.0) == "12 min"


def test_noop_notifier_never_raises():
    NoopNotifier().notify("title", "body")


def test_noop_notifier_ask_consent_returns_default():
    assert NoopNotifier().ask_consent(
        "t", "b", "Yes", "No", 0.1, default_on_timeout="no"
    ) == "no"


def test_desktop_notifier_swallows_init_failure(monkeypatch):
    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("no backend here")

    _install_fake_backend(monkeypatch, _Boom)

    n = DesktopNotifier(app_name="Test")
    n.notify("hi", "there")  # must not raise
    assert n._impl is None


def test_desktop_notifier_swallows_send_failure(monkeypatch):
    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *a, **kw):
            raise RuntimeError("send blew up")

    _install_fake_backend(monkeypatch, _Async)

    n = DesktopNotifier(app_name="Test")
    n.notify("hi", "there")  # must not raise — fire-and-forget path
    time.sleep(0.1)  # let the loop process the scheduled coroutine


def test_desktop_notifier_calls_backend(monkeypatch):
    calls: list[tuple[str, str]] = []
    inits: list[dict] = []

    class _Async:
        def __init__(self, *a, **kw):
            inits.append(kw)

        async def send(self, *, title: str, message: str, **kw):
            calls.append((title, message))

    _install_fake_backend(monkeypatch, _Async)

    n = DesktopNotifier(app_name="Test")
    n.notify("Conversation saved", "Demo · 12 min")
    n.notify("Conversation saved", "Second · 30s")
    # Let the loop actually run the scheduled coroutines.
    time.sleep(0.2)

    assert len(inits) == 1
    assert inits[0]["app_name"] == "Test"
    assert calls == [
        ("Conversation saved", "Demo · 12 min"),
        ("Conversation saved", "Second · 30s"),
    ]


def test_ask_consent_yes(monkeypatch):
    """When the Yes button fires its callback, ask_consent returns 'yes'."""

    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            # Simulate the user clicking Yes.
            if buttons:
                await asyncio.sleep(0)
                buttons[0].on_pressed()

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")
    result = n.ask_consent(
        "Start?", "body", "Yes", "No", timeout_secs=2.0, default_on_timeout="no"
    )
    assert result == "yes"


def test_ask_consent_no(monkeypatch):
    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            if buttons:
                await asyncio.sleep(0)
                buttons[1].on_pressed()

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")
    result = n.ask_consent(
        "Stop?", "body", "Yes", "No", timeout_secs=2.0, default_on_timeout="no"
    )
    assert result == "no"


def test_ask_consent_timeout(monkeypatch):
    """No button pressed within timeout → 'timeout'."""

    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            pass  # never resolve

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")
    result = n.ask_consent(
        "Hmm?", "body", "Yes", "No", timeout_secs=0.2, default_on_timeout="no"
    )
    assert result == "timeout"


def test_noop_notifier_actionable_returns_false_and_calls_expire():
    """Test paths can drive the expire branch via NoopNotifier."""
    expired: list[bool] = []
    pressed: list[bool] = []
    result = NoopNotifier().notify_actionable(
        "t", "b",
        button_label="Open",
        on_pressed=lambda: pressed.append(True),
        expire_after_secs=1.0,
        on_expire=lambda: expired.append(True),
    )
    assert result is False
    assert expired == [True]
    assert pressed == []


def test_noop_notifier_has_authorisation_returns_none():
    assert NoopNotifier().has_authorisation_sync() is None


def test_actionable_press_invokes_on_pressed(monkeypatch):
    """Clicking the button fires on_pressed and cancels the expire timer."""

    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            await asyncio.sleep(0)
            if buttons:
                buttons[0].on_pressed()

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")

    pressed = threading.Event()
    expired = threading.Event()
    dispatched = n.notify_actionable(
        "Daily drill",
        "Body",
        button_label="Open drill",
        on_pressed=lambda: pressed.set(),
        expire_after_secs=2.0,
        on_expire=lambda: expired.set(),
    )
    assert dispatched is True
    assert pressed.wait(timeout=1.0)
    # Allow the timer a moment to fire — it shouldn't because the latch
    # snapped on the press path first.
    time.sleep(0.3)
    assert not expired.is_set()


def test_actionable_expire_fires_when_no_press(monkeypatch):
    """No press within expire_after_secs → on_expire fires exactly once."""

    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            pass  # never invoke on_pressed

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")

    pressed_calls: list[bool] = []
    expired = threading.Event()
    n.notify_actionable(
        "Daily drill",
        "Body",
        button_label="Open drill",
        on_pressed=lambda: pressed_calls.append(True),
        expire_after_secs=0.2,
        on_expire=lambda: expired.set(),
    )
    assert expired.wait(timeout=1.5)
    assert pressed_calls == []


def test_actionable_send_failure_fires_expire(monkeypatch):
    """Backend send failure fires on_expire so the scheduler doesn't hang."""

    class _Async:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title, message, buttons=None, **kw):
            raise RuntimeError("backend boom")

    _install_fake_backend(monkeypatch, _Async)
    n = DesktopNotifier(app_name="Test")

    expired = threading.Event()
    n.notify_actionable(
        "Daily drill",
        "Body",
        button_label="Open drill",
        on_pressed=lambda: None,
        expire_after_secs=10.0,
        on_expire=lambda: expired.set(),
    )
    assert expired.wait(timeout=1.0)


def test_actionable_returns_false_when_backend_unavailable(monkeypatch):
    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("no backend")

    _install_fake_backend(monkeypatch, _Boom)
    n = DesktopNotifier(app_name="Test")
    pressed: list[bool] = []
    expired: list[bool] = []
    result = n.notify_actionable(
        "t", "b",
        button_label="Open",
        on_pressed=lambda: pressed.append(True),
        expire_after_secs=0.1,
        on_expire=lambda: expired.append(True),
    )
    # Backend unavailable returns False AND never fires either callback —
    # the caller (scheduler) is responsible for the EOD fallback path.
    assert result is False
    assert pressed == []
    assert expired == []
