"""Tests for the notifier wrapper + duration formatting."""
from __future__ import annotations

import sys
import types

from sayzo_agent.app import _format_duration
from sayzo_agent.notify import DesktopNotifier, NoopNotifier


def _install_fake_backend(monkeypatch, sync_cls) -> None:
    """Register a fake ``desktop_notifier`` package with a stub ``Icon`` and a
    ``desktop_notifier.sync.DesktopNotifierSync`` pointing at ``sync_cls``."""
    root = types.ModuleType("desktop_notifier")
    root.Icon = type("Icon", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
    sub = types.ModuleType("desktop_notifier.sync")
    sub.DesktopNotifierSync = sync_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "desktop_notifier", root)
    monkeypatch.setitem(sys.modules, "desktop_notifier.sync", sub)


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
    def _boom(*a, **kw):
        raise RuntimeError("no backend here")

    _install_fake_backend(monkeypatch, _boom)

    n = DesktopNotifier(app_name="Test")
    # Must not raise — init failure should leave the notifier a noop.
    n.notify("hi", "there")
    assert n._impl is None


def test_desktop_notifier_swallows_send_failure(monkeypatch):
    class _Sync:
        def __init__(self, *a, **kw):
            pass

        def send(self, *a, **kw):
            raise RuntimeError("send blew up")

    _install_fake_backend(monkeypatch, _Sync)

    n = DesktopNotifier(app_name="Test")
    # Must not raise — send failure is logged and swallowed.
    n.notify("hi", "there")


def test_desktop_notifier_calls_backend(monkeypatch):
    calls: list[tuple[str, str]] = []
    inits: list[dict] = []

    class _Sync:
        def __init__(self, *a, **kw):
            inits.append(kw)

        def send(self, *, title: str, message: str, **kw):
            calls.append((title, message))

    _install_fake_backend(monkeypatch, _Sync)

    n = DesktopNotifier(app_name="Test")
    n.notify("Conversation saved", "Demo · 12 min")
    n.notify("Conversation saved", "Second · 30s")

    # Backend should be constructed exactly once (lazy + cached), and both
    # sends should go through it — confirming we reuse the persistent loop
    # instead of spinning up a fresh one per call.
    assert len(inits) == 1
    assert inits[0]["app_name"] == "Test"
    assert calls == [
        ("Conversation saved", "Demo · 12 min"),
        ("Conversation saved", "Second · 30s"),
    ]
