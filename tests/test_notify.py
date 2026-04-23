"""Tests for the notifier wrapper + duration formatting.

The notifier spins up a dedicated asyncio loop on a daemon thread. Tests
patch ``desktop_notifier.DesktopNotifier`` with an async fake before
constructing ``sayzo_agent.notify.DesktopNotifier`` so no real OS toast
surface is hit.
"""
from __future__ import annotations

import asyncio
import sys
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
