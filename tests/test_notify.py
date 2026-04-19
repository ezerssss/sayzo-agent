"""Tests for the notifier wrapper + duration formatting."""
from __future__ import annotations

from sayzo_agent.app import _format_duration
from sayzo_agent.notify import DesktopNotifier, NoopNotifier


def test_format_duration_subminute():
    assert _format_duration(12.4) == "12s"
    assert _format_duration(59.4) == "59s"


def test_format_duration_minutes():
    assert _format_duration(60.0) == "1 min"
    assert _format_duration(90.0) == "2 min"  # rounds
    assert _format_duration(725.0) == "12 min"


def test_noop_notifier_never_raises():
    NoopNotifier().notify("title", "body")


def test_desktop_notifier_swallows_init_failure(monkeypatch):
    import sayzo_agent.notify as notify_mod

    def _boom(*a, **kw):
        raise RuntimeError("no backend here")

    # Simulate desktop_notifier import succeeding but constructor failing.
    import types
    fake = types.ModuleType("desktop_notifier")
    fake.DesktopNotifier = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "desktop_notifier", fake)

    n = DesktopNotifier(app_name="Test")
    # Must not raise — init failure should leave the notifier a noop.
    n.notify("hi", "there")
    assert n._impl is None


def test_desktop_notifier_swallows_send_failure(monkeypatch):
    class _Backend:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *a, **kw):
            raise RuntimeError("send blew up")

    import types
    fake = types.ModuleType("desktop_notifier")
    fake.DesktopNotifier = _Backend  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "desktop_notifier", fake)

    n = DesktopNotifier(app_name="Test")
    # Must not raise — send failure is logged and swallowed.
    n.notify("hi", "there")


def test_desktop_notifier_calls_backend(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Backend:
        def __init__(self, *a, **kw):
            pass

        async def send(self, *, title: str, message: str, **kw):
            calls.append((title, message))

    import types
    fake = types.ModuleType("desktop_notifier")
    fake.DesktopNotifier = _Backend  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "desktop_notifier", fake)

    n = DesktopNotifier(app_name="Test")
    n.notify("Conversation saved", "Demo \u00b7 12 min")

    assert calls == [("Conversation saved", "Demo \u00b7 12 min")]
