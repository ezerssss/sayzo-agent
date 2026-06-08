"""Tests for the notifier wrappers in v2.10+.

Native notifications are gone — every toast routes through the custom
HUD subprocess managed by :class:`HudLauncher`. These tests cover:

* :class:`NoopNotifier` — unchanged.
* :class:`HudNotifier` — wraps a launcher; verify each Notifier method
  forwards to the right launcher method with the expected arguments.
* ``_format_duration`` — unchanged.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from sayzo_agent.app import _format_duration
from sayzo_agent.notify import HudNotifier, NoopNotifier


# ----------------------------------------------------------------------
# Format-duration regressions (unchanged).
# ----------------------------------------------------------------------

def test_format_duration_subminute():
    assert _format_duration(12.4) == "12s"
    assert _format_duration(59.4) == "59s"


def test_format_duration_minutes():
    assert _format_duration(60.0) == "1 min"
    assert _format_duration(90.0) == "2 min"  # rounds
    assert _format_duration(725.0) == "12 min"


# ----------------------------------------------------------------------
# NoopNotifier.
# ----------------------------------------------------------------------

def test_noop_notifier_never_raises():
    NoopNotifier().notify("title", "body")


def test_noop_notifier_ask_consent_returns_default():
    assert NoopNotifier().ask_consent(
        "t", "b", "Yes", "No", 0.1, default_on_timeout="no"
    ) == "no"


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


# ----------------------------------------------------------------------
# HudNotifier — round-trips through a fake launcher.
# ----------------------------------------------------------------------

class _FakeLauncher:
    """Hand-rolled stand-in for HudLauncher.

    Captures every call into ``calls`` so tests can assert. Lets a test
    pre-program the consent answer via ``set_consent_answer`` and the
    actionable outcome via ``set_actionable_outcome``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._consent_answer: str = "no"
        self._actionable_outcome: Optional[str] = None
        self._alive = True

    # Programming knobs.

    def set_consent_answer(self, answer: str) -> None:
        self._consent_answer = answer

    def set_actionable_outcome(self, outcome: Optional[str]) -> None:
        """``None`` = neither fires (caller wants to test that nothing happens)."""
        self._actionable_outcome = outcome

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    # HudLauncher-shaped surface.

    def show_toast(self, title: str, body: str, ttl_secs: float = 4.0) -> bool:
        self.calls.append((
            "show_toast",
            {"title": title, "body": body, "ttl_secs": ttl_secs},
        ))
        return True

    def ask_consent(
        self,
        *,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: str = "no",
        supersede: bool = False,
    ) -> str:
        self.calls.append((
            "ask_consent",
            {
                "title": title,
                "body": body,
                "yes_label": yes_label,
                "no_label": no_label,
                "timeout_secs": timeout_secs,
                "default_on_timeout": default_on_timeout,
                "supersede": supersede,
            },
        ))
        return self._consent_answer

    def show_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        self.calls.append((
            "show_actionable",
            {
                "title": title,
                "body": body,
                "button_label": button_label,
                "expire_after_secs": expire_after_secs,
                "secondary_button_label": secondary_button_label,
            },
        ))
        if self._actionable_outcome == "pressed":
            on_pressed()
        elif self._actionable_outcome == "snoozed" and on_secondary_pressed is not None:
            on_secondary_pressed()
        elif self._actionable_outcome == "expired" and on_expire is not None:
            on_expire()
        return True

    def is_alive(self) -> bool:
        return self._alive

    def diagnose(self) -> dict[str, Any]:
        return {"alive": self._alive}


def test_hud_notifier_notify_forwards_to_show_toast():
    fake = _FakeLauncher()
    notifier = HudNotifier(fake)
    notifier.notify("Conversation saved", "Demo · 12 min")
    assert len(fake.calls) == 1
    name, kwargs = fake.calls[0]
    assert name == "show_toast"
    assert kwargs["title"] == "Conversation saved"
    assert kwargs["body"] == "Demo · 12 min"


def test_hud_notifier_ask_consent_returns_yes(monkeypatch):
    fake = _FakeLauncher()
    fake.set_consent_answer("yes")
    notifier = HudNotifier(fake)
    answer = notifier.ask_consent(
        "Start?", "body", "Yes", "No", timeout_secs=5.0, default_on_timeout="no",
    )
    assert answer == "yes"
    name, kwargs = fake.calls[0]
    assert name == "ask_consent"
    assert kwargs["title"] == "Start?"
    assert kwargs["yes_label"] == "Yes"
    assert kwargs["default_on_timeout"] == "no"


def test_hud_notifier_ask_consent_returns_no():
    fake = _FakeLauncher()
    fake.set_consent_answer("no")
    answer = HudNotifier(fake).ask_consent(
        "Stop?", "body", "Yes", "No", 5.0, "no",
    )
    assert answer == "no"


def test_hud_notifier_ask_consent_propagates_timeout():
    fake = _FakeLauncher()
    fake.set_consent_answer("timeout")
    answer = HudNotifier(fake).ask_consent(
        "Hmm?", "body", "Yes", "No", 0.2, "no",
    )
    assert answer == "timeout"


def test_hud_notifier_actionable_press():
    fake = _FakeLauncher()
    fake.set_actionable_outcome("pressed")
    pressed = threading.Event()
    expired = threading.Event()
    notifier = HudNotifier(fake)
    dispatched = notifier.notify_actionable(
        "Conversation saved",
        "Body",
        button_label="Open in Sayzo",
        on_pressed=lambda: pressed.set(),
        expire_after_secs=2.0,
        on_expire=lambda: expired.set(),
    )
    assert dispatched is True
    assert pressed.is_set()
    assert not expired.is_set()


def test_hud_notifier_actionable_expire():
    fake = _FakeLauncher()
    fake.set_actionable_outcome("expired")
    pressed: list[bool] = []
    expired = threading.Event()
    HudNotifier(fake).notify_actionable(
        "Conversation saved",
        "Body",
        button_label="Open in Sayzo",
        on_pressed=lambda: pressed.append(True),
        expire_after_secs=0.2,
        on_expire=lambda: expired.set(),
    )
    assert expired.is_set()
    assert pressed == []


def test_hud_notifier_actionable_secondary_button_forwards():
    """v3.8.x: the optional 'Snooze 1h' secondary button + its callback
    forward through HudNotifier → launcher, and a 'snoozed' outcome fires
    on_secondary_pressed (not on_pressed / on_expire)."""
    fake = _FakeLauncher()
    fake.set_actionable_outcome("snoozed")
    snoozed = threading.Event()
    pressed: list[bool] = []
    expired: list[bool] = []
    HudNotifier(fake).notify_actionable(
        "Conversation saved",
        "Body",
        button_label="Open in Sayzo",
        on_pressed=lambda: pressed.append(True),
        expire_after_secs=2.0,
        on_expire=lambda: expired.append(True),
        secondary_button_label="Snooze 1h",
        on_secondary_pressed=lambda: snoozed.set(),
    )
    assert snoozed.is_set()
    assert pressed == []
    assert expired == []
    name, kwargs = fake.calls[0]
    assert name == "show_actionable"
    assert kwargs["secondary_button_label"] == "Snooze 1h"


def test_hud_notifier_actionable_no_secondary_button_by_default():
    """Single-button actionables still forward with secondary=None."""
    fake = _FakeLauncher()
    fake.set_actionable_outcome("pressed")
    HudNotifier(fake).notify_actionable(
        "Conversation saved",
        "Body",
        button_label="Open in Sayzo",
        on_pressed=lambda: None,
        expire_after_secs=2.0,
    )
    _, kwargs = fake.calls[0]
    assert kwargs["secondary_button_label"] is None


def test_hud_notifier_has_authorisation_when_alive():
    fake = _FakeLauncher()
    fake.set_alive(True)
    assert HudNotifier(fake).has_authorisation_sync() is True


def test_hud_notifier_has_authorisation_none_when_dead():
    fake = _FakeLauncher()
    fake.set_alive(False)
    # When the launcher has given up (is_alive=False), authorisation is
    # "unknown" — callers treat None as "skip the silent-drop fallback"
    # since native notifications no longer exist.
    assert HudNotifier(fake).has_authorisation_sync() is None
