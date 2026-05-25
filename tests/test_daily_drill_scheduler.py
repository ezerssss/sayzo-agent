"""Integration tests for ``DailyDrillScheduler``.

Wires up a ``FakeNotifier`` with explicit ``simulate_press`` /
``simulate_expire`` hooks so we can drive the scheduler's outcome
flow without a real toast surface. Time is frozen via ``now_fn``;
the model is seeded by direct ``record_fire`` / ``record_outcome``
calls; the API client is a stub returning canned responses.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest

from sayzo_agent.config import NotificationConfig
from sayzo_agent.daily_drill import scheduler as scheduler_mod
from sayzo_agent.daily_drill.api import TodaySessionResponse
from sayzo_agent.daily_drill.scheduler import DailyDrillScheduler


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeArmStateValue:
    value: str

    def __eq__(self, other: object) -> bool:
        if hasattr(other, "value"):
            return self.value == getattr(other, "value")
        return self.value == other

    def __hash__(self) -> int:
        return hash(self.value)


_DISARMED = _FakeArmStateValue("disarmed")
_ARMED = _FakeArmStateValue("armed")


class FakeNotifier:
    """Captures notify_actionable calls and exposes simulate_press /
    simulate_expire to drive the latched callback flow."""

    def __init__(
        self, *, dispatch_returns: bool = True, has_authorisation: Optional[bool] = True,
    ) -> None:
        self.dispatch_returns = dispatch_returns
        self.has_authorisation = has_authorisation
        self.actionable_calls: list[dict[str, Any]] = []
        self.notify_calls: list[tuple[str, str]] = []
        self._latch = False
        self._on_pressed: Optional[Callable[[], None]] = None
        self._on_expire: Optional[Callable[[], None]] = None
        self._on_secondary: Optional[Callable[[], None]] = None

    def notify(self, title: str, body: str) -> None:
        self.notify_calls.append((title, body))

    def notify_actionable(
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
        self.actionable_calls.append(
            dict(
                title=title,
                body=body,
                button_label=button_label,
                expire_after_secs=expire_after_secs,
                secondary_button_label=secondary_button_label,
            )
        )
        if not self.dispatch_returns:
            return False
        self._latch = False
        self._on_pressed = on_pressed
        self._on_expire = on_expire
        self._on_secondary = on_secondary_pressed
        return True

    def has_authorisation_sync(self) -> Optional[bool]:
        return self.has_authorisation

    def ask_consent(self, *args, **kwargs):
        return "no"

    def simulate_press(self) -> None:
        if self._latch or self._on_pressed is None:
            return
        self._latch = True
        self._on_pressed()

    def simulate_expire(self) -> None:
        if self._latch or self._on_expire is None:
            return
        self._latch = True
        self._on_expire()

    def simulate_snooze(self) -> None:
        if self._latch or self._on_secondary is None:
            return
        self._latch = True
        self._on_secondary()


class FakeAuthClient:
    """Dummy AuthenticatedClient returning a fixed response."""

    def __init__(self, response: TodaySessionResponse) -> None:
        self.response = response
        self.calls = 0

    async def get(self, path: str, **kwargs) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _ok_response() -> TodaySessionResponse:
    return TodaySessionResponse(
        status="ok",
        session_id="sess_test",
        deep_link_url="https://sayzo.app/drills/sess_test",
        is_replay=False,
        scenario_title="Monday standup",
        question="Give your standup in 60 seconds.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def stats_path(tmp_path: Path) -> Path:
    return tmp_path / "stats.json"


def _cfg(**overrides) -> NotificationConfig:
    base = dict(
        daily_drill_enabled=True,
        min_idle_secs=30.0,
        max_idle_secs=600.0,
        cooldown_secs=18 * 3600.0,
        boot_warmup_secs=30.0,
        cold_start_hour=11,
        min_hour=9,
        lunch_start_hour=12,
        lunch_end_hour=13,
        max_hour=20,
        eod_fallback_hour=17,
        dismiss_window_secs=300.0,
        soft_tap_window_secs=14400.0,
        prior_alpha=3.0,
        recency_decay=0.95,
        bad_score_threshold=0.05,
        max_history=200,
        tick_secs=60.0,
    )
    base.update(overrides)
    return NotificationConfig(**base)


def _make_scheduler(
    *,
    stats_path: Path,
    cfg: Optional[NotificationConfig] = None,
    master_enabled: bool = True,
    has_tokens: bool = True,
    arm_state: _FakeArmStateValue = _DISARMED,
    mic_active: bool = False,
    idle_secs: float = 600.0,
    response: Optional[TodaySessionResponse] = None,
    fake_notifier: Optional[FakeNotifier] = None,
    auth_client_returns_none: bool = False,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> tuple[DailyDrillScheduler, FakeNotifier, list[str]]:
    """Build a scheduler with all the right stubs.

    ``response`` (when not None) is what the FakeAuthClient returns.
    Returns (scheduler, notifier, opened_urls).
    """
    cfg = cfg or _cfg()
    notifier = fake_notifier or FakeNotifier()
    response = response or _ok_response()

    token_store = MagicMock()
    token_store.has_tokens = lambda: has_tokens

    fake_client = FakeAuthClient(response)

    # Patch fetch_today_session at module level so we don't need a real
    # AuthenticatedClient.
    if auth_client_returns_none:
        auth_client_factory = lambda: None
    else:
        auth_client_factory = lambda: fake_client

    opened_urls: list[str] = []

    sched = DailyDrillScheduler(
        cfg=cfg,
        master_notifications_enabled=master_enabled,
        stats_path=stats_path,
        notifier=notifier,
        token_store=token_store,
        auth_client_factory=auth_client_factory,
        arm_state_fn=lambda: arm_state,
        is_mic_active_fn=lambda: mic_active,
        get_idle_secs_fn=lambda: idle_secs,
        tray_state=None,
        now_fn=lambda: now or datetime(2026, 5, 4, 11, 5),  # Mon 11:05
        rng=rng or random.Random(0),
        tick_secs=60.0,
    )
    sched._started_at_mono = time.monotonic() - cfg.boot_warmup_secs - 60.0
    sched._has_auth_cached = notifier.has_authorisation
    return sched, notifier, opened_urls


def _patch_fetch(monkeypatch, response: TodaySessionResponse) -> list[Any]:
    """Replace ``fetch_today_session`` with a stub that returns ``response``."""
    calls: list[Any] = []

    async def fake_fetch(client, **kwargs):
        calls.append(client)
        return response

    monkeypatch.setattr(scheduler_mod, "fetch_today_session", fake_fetch)
    return calls


def _patch_open(monkeypatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(
        scheduler_mod.webbrowser, "open", lambda url: opened.append(url) or True,
    )
    return opened


# ---------------------------------------------------------------------------
# Gate tests — each verifies a single skip condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_master_disabled(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, master_enabled=False
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_master_disabled"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_skips_when_daily_drill_disabled(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, cfg=_cfg(daily_drill_enabled=False)
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_daily_disabled"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_skips_when_no_tokens(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, has_tokens=False)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_unauthenticated"
    assert notifier.actionable_calls == []


# Pre-v2.10 the scheduler skipped daily-drill firing and surfaced a
# one-time tray warning when has_authorisation_sync() returned False
# (OS notifications disabled at the system level). With the v2.10 HUD
# rewrite, has_authorisation_sync() never returns False — the HUD owns
# its own UI surface and is "authorised" whenever the subprocess is
# alive. The corresponding "test_skips_when_os_notifications_disabled"
# test has been removed because the code path it exercised no longer
# exists; see project_custom_hud_shipped.md for the migration notes.


@pytest.mark.asyncio
async def test_skips_during_arm(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, arm_state=_ARMED
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_armed"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_skips_when_mic_active(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, mic_active=True
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_mic_active"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_skips_when_idle_below_threshold(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, idle_secs=10.0)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_idle"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_lunch_hour_no_longer_blocks(stats_path, monkeypatch) -> None:
    """v3.6.7: time-of-day gates removed. Lunch hour was a heuristic for
    'user is probably away'; the activity gate (max_idle_secs) handles
    that directly now. At 12:30 with idle within the activity window,
    fires normally."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 12, 30),
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_before_min_hour_no_longer_blocks(stats_path, monkeypatch) -> None:
    """v3.6.7: 7 AM (pre-9 AM in the old workday window) fires normally
    if the user is actively present. The activity gate replaces the
    time-of-day window — schedule-agnostic by design."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 7, 0),
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_night_shift_hour_fires_normally(stats_path, monkeypatch) -> None:
    """v3.6.7 the headline use case: a PH BPO worker at 23:00 PHT (night
    shift) sees a toast just like a daytime worker at 11:00 — same gates,
    same outcome. No SAYZO_NOTIFICATIONS__* configuration required."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 23, 0),
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_skips_when_idle_above_max(stats_path, monkeypatch) -> None:
    """The night-shift gate: a sleeping user is idle for many hours,
    far above max_idle_secs (default 600s). Toast skipped — this is what
    makes the activity gate work for any schedule without time-of-day
    knowledge."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, idle_secs=3600.0)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_idle"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_skips_when_in_cooldown(stats_path, monkeypatch) -> None:
    """v3.6.7: cooldown replaces last_fired_on_day == today. A fire 1 hour
    ago should still block any new fire (default cooldown=18h)."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 14, 0),
    )
    # Pretend we fired one hour ago.
    sched._stats.last_fire_at = datetime(2026, 5, 4, 13, 0).isoformat()
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_already_fired"
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_fires_when_cooldown_expired(stats_path, monkeypatch) -> None:
    """20 hours after the last fire (cooldown=18h), eligible to fire again."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 14, 0),
    )
    sched._stats.last_fire_at = datetime(2026, 5, 3, 18, 0).isoformat()  # 20h ago
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_skips_within_boot_warmup(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, _, _ = _make_scheduler(stats_path=stats_path)
    sched._started_at_mono = time.monotonic()  # just booted
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_boot_warmup"


@pytest.mark.asyncio
async def test_fires_on_weekend_with_no_history(stats_path, monkeypatch) -> None:
    """v3.6.5: weekend cold-start gate dropped.

    Pre-v3.6.5 a new user got zero toasts on Sat/Sun until they engaged
    with one — which they couldn't, because they never got one. The gate
    was deleted; weekends now behave the same as weekdays.
    """
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 9, 11, 5),  # Saturday
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


# ---------------------------------------------------------------------------
# Fire flow + outcome recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_records_fire_and_marks_day(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert res.title == "Your Monday standup drill is ready — 60s"
    assert sched._stats.last_fired_on_day == "2026-05-04"
    # Bucket fires bumped exactly once.
    assert sched._model.total_fires() == 1


@pytest.mark.asyncio
async def test_fires_only_once_per_day(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    first = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    second = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert first.status == "fired"
    assert second.status == "skipped_already_fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_over_credit_defensively_skipped_as_transient(
    stats_path, monkeypatch,
) -> None:
    """v3.6.5: platform confirmed `/sessions/today` never returns 402.

    The `over_credit` status is dead in api.py. If a future regression
    somehow surfaced one, the defensive non-fireable branch in _fire_drill
    treats it as transient (re-fetch next tick) rather than locking the
    day. This test pins that behavior so a regression makes a loud noise.
    """
    _patch_fetch(
        monkeypatch, TodaySessionResponse(status="over_credit")
    )
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_transient_error"
    # Day NOT marked — would retry next tick.
    assert sched._stats.last_fired_on_day is None
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_still_processing_with_no_deeplink_does_not_fire(
    stats_path, monkeypatch,
) -> None:
    """409 DRILL_STILL_PROCESSING omits deepLinkUrl per server contract.

    Pre-v3.6.5 we still fired a toast that did nothing on click. The
    new guard returns skipped_no_deep_link so the user doesn't see a
    broken-feeling notification; the next tick re-fetches and the
    server will have finished processing by then.
    """
    _patch_fetch(
        monkeypatch,
        TodaySessionResponse(
            status="still_processing",
            session_id="sess_pending",
            deep_link_url=None,
        ),
    )
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_no_deep_link"
    assert sched._stats.last_fired_on_day is None
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_retry_required_with_deeplink_still_fires(
    stats_path, monkeypatch,
) -> None:
    """409 DRILL_RETRY_REQUIRED includes deepLinkUrl and should fire.

    The user's last drill needs a redo; the toast click takes them to
    the redo page. Companion to the still_processing test above.
    """
    _patch_fetch(
        monkeypatch,
        TodaySessionResponse(
            status="retry_required",
            session_id="sess_redo",
            deep_link_url="https://sayzo.app/drills/sess_redo",
            scenario_title="Wednesday standup",
        ),
    )
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_5xx_silent_skip(stats_path, monkeypatch) -> None:
    _patch_fetch(
        monkeypatch, TodaySessionResponse(status="transient_error")
    )
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_transient_error"
    # Day is NOT marked — we'll retry next tick.
    assert sched._stats.last_fired_on_day is None
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_tap_records_tap_outcome(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    opened = _patch_open(monkeypatch)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_press()
    # Outcome recorded; bucket has 1 tap, 0 expires.
    bucket = sched._model.stats.buckets["0-11"]  # Mon 11
    assert bucket.fires == 1
    assert bucket.taps == 1
    assert bucket.expires == 0
    # Browser opened with deep link.
    assert opened == ["https://sayzo.app/drills/sess_test"]


@pytest.mark.asyncio
async def test_expire_records_expire_outcome(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    opened = _patch_open(monkeypatch)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_expire()
    bucket = sched._model.stats.buckets["0-11"]
    assert bucket.fires == 1
    assert bucket.taps == 0
    assert bucket.expires == 1
    assert opened == []


# ---------------------------------------------------------------------------
# EOD fallback (v3.6.5: real toast, tray as last-resort)
# ---------------------------------------------------------------------------


class FakeTrayState:
    def __init__(self) -> None:
        self.label: Optional[str] = None

    def set_eod_drill_label(self, label: Optional[str]) -> None:
        self.label = label

    def get_eod_drill_label(self) -> Optional[str]:
        return self.label


@pytest.mark.asyncio
async def test_dispatch_failed_in_hours_sets_tray_label_directly(
    stats_path, monkeypatch,
) -> None:
    """In-hours dispatch failure: skip the EOD re-entry, just set tray.

    Pre-v3.6.5 the dispatch-failure path called _maybe_surface_eod_fallback
    which (now that EOD also tries a toast) would just retry the same
    failing dispatch. v3.6.5 sets the tray label directly via the helper.
    """
    _patch_fetch(monkeypatch, _ok_response())
    fn = FakeNotifier(dispatch_returns=False)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(stats_path=stats_path, fake_notifier=fn)
    sched._tray_state = tray  # type: ignore[assignment]
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_dispatch_failed"
    assert sched._stats.last_fired_on_day == "2026-05-04"
    assert tray.label == "Today's drill — 60s"


@pytest.mark.asyncio
async def test_dispatch_failure_falls_back_to_tray(
    stats_path, monkeypatch,
) -> None:
    """v3.6.7: the EOD path (which used to fire from now.hour >= max_hour)
    is gone. The tray fallback only triggers when notify_actionable
    returns False (HUD subprocess crashed, etc.)."""
    _patch_fetch(monkeypatch, _ok_response())
    fn = FakeNotifier(dispatch_returns=False)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(stats_path=stats_path, fake_notifier=fn)
    sched._tray_state = tray  # type: ignore[assignment]
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_dispatch_failed"
    assert tray.label == "Today's drill — 60s"
    assert sched._stats.eod_fallback_shown_on == "2026-05-04"
    # Cooldown is also marked so we don't keep retrying every tick.
    assert sched._stats.last_fire_at is not None


@pytest.mark.asyncio
async def test_does_not_fire_twice_within_cooldown(
    stats_path, monkeypatch,
) -> None:
    """Cooldown gate: second tick same day must NOT fire a second toast."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert len(notifier.actionable_calls) == 1
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_tray_click_opens_link_and_records_soft_tap(
    stats_path, monkeypatch,
) -> None:
    """When dispatch fails and falls back to the tray, the user's
    tray-click still opens the drill + records soft_tap (preserved from
    v3.6.5 — it's the user's escape hatch from a broken HUD)."""
    _patch_fetch(monkeypatch, _ok_response())
    opened = _patch_open(monkeypatch)
    fn = FakeNotifier(dispatch_returns=False)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(stats_path=stats_path, fake_notifier=fn)
    sched._tray_state = tray  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    sched.on_eod_tray_click()
    assert opened == ["https://sayzo.app/drills/sess_test"]
    bucket = sched._model.stats.buckets["0-11"]  # Mon 11 — the default test now
    assert bucket.soft_taps == 1
    assert tray.label is None


# ---------------------------------------------------------------------------
# fire_now (test trigger) bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_bypasses_gates(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path,
        idle_secs=0.0,  # would normally fail idle gate
        arm_state=_ARMED,  # would normally fail arm gate
        mic_active=True,  # would normally fail mic gate
        now=datetime(2026, 5, 4, 7, 0),  # before min_hour
    )
    res = await sched.fire_now(ignore_gates=True)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_fire_now_still_requires_auth(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, has_tokens=False,
    )
    res = await sched.fire_now(ignore_gates=True)
    assert res.status == "skipped_unauthenticated"
    assert notifier.actionable_calls == []


# ---------------------------------------------------------------------------
# Reload config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_config_disables_takes_effect_next_tick(
    stats_path, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    # First call fires.
    res1 = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res1.status == "fired"
    # Reload with daily disabled; subsequent call (after cooldown
    # cleared) skips.
    sched.reload_config(_cfg(daily_drill_enabled=False), master_enabled=True)
    sched._stats.last_fire_at = None  # simulate cooldown expiry
    sched._stats.last_fired_on_day = None
    sched._last_seen_day = None
    res2 = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res2.status == "skipped_daily_disabled"


# ---------------------------------------------------------------------------
# Cross-midnight reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_midnight_reset_allows_next_day_fire(
    stats_path, monkeypatch
) -> None:
    """After firing on day N, the scheduler fires again on day N+1.

    Day 2 is set to 16:30 so the only candidate hour is 16; model
    picks deterministically without rng coupling.
    """
    _patch_fetch(monkeypatch, _ok_response())
    times = [
        datetime(2026, 5, 4, 11, 5),   # Monday 11:05 (cold start picks 11)
        datetime(2026, 5, 5, 16, 30),  # Tuesday 16:30 (only candidate is 16)
    ]
    idx = {"i": 0}

    def now_fn() -> datetime:
        return times[idx["i"]]

    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=times[0],
    )
    sched._now_fn = now_fn  # type: ignore[assignment]

    res1 = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res1.status == "fired"
    idx["i"] = 1
    res2 = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res2.status == "fired"
    assert len(notifier.actionable_calls) == 2


# ---------------------------------------------------------------------------
# First-fire on agent start (v3.6.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_fire_on_start_fires_after_warmup(
    stats_path, monkeypatch,
) -> None:
    """v3.6.5: a one-shot first-fire runs after boot_warmup_secs + 5s
    so a user launching the agent at noon and quitting at 5pm always
    sees a toast that day, even if Thompson sampling would defer."""
    _patch_fetch(monkeypatch, _ok_response())
    # Short warmup so the test doesn't wait 30 seconds.
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, cfg=cfg)
    # Reset _started_at_mono so the boot_warmup branch in _evaluate
    # (which runs inside ignore_gates=True path) doesn't pre-fire.
    sched._started_at_mono = time.monotonic()
    # Manually trigger the first-fire task body.
    await sched._maybe_first_fire()
    assert len(notifier.actionable_calls) == 1
    assert sched._stats.last_fired_on_day == "2026-05-04"


@pytest.mark.asyncio
async def test_first_fire_on_start_skipped_if_in_cooldown(
    stats_path, monkeypatch,
) -> None:
    """First-fire honors cooldown even though it ignores other gates.
    Pretend a tick fired 2 hours ago — cooldown is still active (18h)."""
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, cfg=cfg, now=datetime(2026, 5, 4, 13, 0),
    )
    sched._stats.last_fire_at = datetime(2026, 5, 4, 11, 0).isoformat()
    await sched._maybe_first_fire()
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_first_fire_on_start_skipped_if_unauthenticated(
    stats_path, monkeypatch,
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, cfg=cfg, has_tokens=False,
    )
    await sched._maybe_first_fire()
    # Even with ignore_gates=True the auth gate is honored.
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_first_fire_on_start_cancelled_during_warmup(
    stats_path, monkeypatch,
) -> None:
    """Calling stop() during warmup must not produce a stray toast."""
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=60.0)  # long enough that we never reach it
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, cfg=cfg)
    # Set the stop event to simulate shutdown mid-warmup.
    sched._stop.set()
    await sched._maybe_first_fire()
    assert notifier.actionable_calls == []


@pytest.mark.asyncio
async def test_first_fire_on_start_skipped_if_armed(
    stats_path, monkeypatch,
) -> None:
    """First-fire must NOT dispatch while the agent is capturing a call.

    Regression guard: ignore_gates=True inside _evaluate_and_maybe_fire
    bypasses the in-chain armed/mic checks, so _maybe_first_fire has to
    pre-check them explicitly. Without this guard, launching Sayzo right
    before a Zoom call produces a toast mid-meeting.
    """
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, cfg=cfg, arm_state=_ARMED,
    )
    await sched._maybe_first_fire()
    assert notifier.actionable_calls == []
    assert sched._stats.last_fired_on_day is None


@pytest.mark.asyncio
async def test_first_fire_on_start_skipped_if_mic_active(
    stats_path, monkeypatch,
) -> None:
    """Companion to the armed-skip test: mic-held by any process also
    blocks first-fire (e.g., user in a web meeting Sayzo isn't tracking)."""
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, cfg=cfg, mic_active=True,
    )
    await sched._maybe_first_fire()
    assert notifier.actionable_calls == []
    assert sched._stats.last_fired_on_day is None


# ---------------------------------------------------------------------------
# Snooze button (v3.8.x)
# ---------------------------------------------------------------------------
#
# The daily-drill toast carries a "Snooze 1h" secondary button. Clicking it
# defers the drill within the same day (re-fire after snooze_duration_secs)
# instead of losing it. One snooze per drill; if the user is busy at the
# re-fire moment we keep retrying until snooze_max_defer_secs, then fall back
# to the EOD tray. We deliberately did NOT tighten the activity gate — see
# the v3.6.7 pivot (over-conservative gating = the toast never fires at all).


@pytest.mark.asyncio
async def test_drill_toast_offers_snooze_button(stats_path, monkeypatch) -> None:
    """The original fire carries the 'Snooze 1h' secondary button."""
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert len(notifier.actionable_calls) == 1
    assert notifier.actionable_calls[0]["secondary_button_label"] == "Snooze 1h"


@pytest.mark.asyncio
async def test_snooze_records_outcome_and_sets_pending_snooze_until(
    stats_path, monkeypatch,
) -> None:
    """Snooze click → outcome 'snooze', re-fire deadline = now+1h.

    last_fire_at must NOT move — it still reflects the original toast so
    first-fire-on-start stays cooldown-blocked and the tick loop owns the
    re-fire.
    """
    _patch_fetch(monkeypatch, _ok_response())
    now = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=now)
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    bucket = sched._model.stats.buckets["0-11"]
    assert bucket.snoozes == 1
    assert bucket.taps == 0
    assert bucket.expires == 0
    assert sched._stats.pending_snooze_until == (now + timedelta(hours=1)).isoformat()
    assert sched._stats.last_fire_at == now.isoformat()


@pytest.mark.asyncio
async def test_snooze_does_not_refire_before_duration(
    stats_path, monkeypatch,
) -> None:
    """Within the 1h snooze window the scheduler stays quiet."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    clock["now"] = t0 + timedelta(minutes=30)  # still inside the window
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_snoozed"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_snooze_refires_after_duration_when_idle(
    stats_path, monkeypatch,
) -> None:
    """Past the 1h window with the user present, the drill re-fires once.

    The re-fire is the single allowed snooze → its toast offers no
    secondary button (can't chain into 'never fires').
    """
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    assert len(notifier.actionable_calls) == 1
    clock["now"] = t0 + timedelta(hours=1, seconds=1)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier.actionable_calls) == 2
    assert notifier.actionable_calls[1]["secondary_button_label"] is None
    assert sched._stats.pending_snooze_until is None


@pytest.mark.asyncio
async def test_snooze_refire_deferred_when_armed(stats_path, monkeypatch) -> None:
    """If the user is in a meeting at the re-fire moment, defer (don't drop)."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    armed = {"v": _DISARMED}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    sched._arm_state_fn = lambda: armed["v"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    clock["now"] = t0 + timedelta(hours=1, seconds=1)  # past deadline …
    armed["v"] = _ARMED  # … but now in a meeting
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_armed"
    assert len(notifier.actionable_calls) == 1
    # Still pending — the next idle tick will re-fire.
    assert sched._stats.pending_snooze_until is not None


@pytest.mark.asyncio
async def test_snooze_refire_drops_to_eod_after_max_defer(
    stats_path, monkeypatch,
) -> None:
    """Busy past snooze_max_defer_secs → give up to the quiet EOD tray."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    tray = FakeTrayState()
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    sched._tray_state = tray  # type: ignore[assignment]
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    # 1h window + 4h max defer + slack.
    clock["now"] = t0 + timedelta(hours=6)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_snooze_gave_up"
    assert sched._stats.pending_snooze_until is None
    assert tray.label == "Today's drill — 60s"
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_snooze_bypasses_cooldown_at_refire(stats_path, monkeypatch) -> None:
    """The original fire's 18h cooldown must NOT block the 1h re-fire."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert sched._stats.last_fire_at == t0.isoformat()
    notifier.simulate_snooze()
    assert sched._stats.last_fire_at == t0.isoformat()  # unchanged by snooze
    clock["now"] = t0 + timedelta(hours=1, seconds=1)  # deep inside 18h cooldown
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    # The re-fire advanced the cooldown clock.
    assert sched._stats.last_fire_at == (t0 + timedelta(hours=1, seconds=1)).isoformat()


@pytest.mark.asyncio
async def test_snooze_persists_across_scheduler_restart(
    stats_path, monkeypatch,
) -> None:
    """A pending snooze survives an agent restart — it lives in stats.json."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched1, notifier1, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock1 = {"now": t0}
    sched1._now_fn = lambda: clock1["now"]  # type: ignore[assignment]
    await sched1._evaluate_and_maybe_fire(ignore_gates=False)
    notifier1.simulate_snooze()
    assert sched1._stats.pending_snooze_until is not None

    # Fresh scheduler reads the same stats file; advance past the deadline.
    t1 = t0 + timedelta(hours=1, seconds=1)
    sched2, notifier2, _ = _make_scheduler(stats_path=stats_path, now=t1)
    assert sched2._stats.pending_snooze_until == (t0 + timedelta(hours=1)).isoformat()
    res = await sched2._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "fired"
    assert len(notifier2.actionable_calls) == 1


@pytest.mark.asyncio
async def test_manual_fire_while_snooze_pending_clears_it(
    stats_path, monkeypatch,
) -> None:
    """fire_now (ignore_gates) while a snooze is armed must consume the
    pending deadline, or the tick loop would re-fire a duplicate later."""
    _patch_fetch(monkeypatch, _ok_response())
    t0 = datetime(2026, 5, 4, 11, 5)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    assert sched._stats.pending_snooze_until is not None
    # Manual trigger 30 min later (still inside the snooze window).
    clock["now"] = t0 + timedelta(minutes=30)
    res = await sched.fire_now(ignore_gates=True)
    assert res.status == "fired"
    # Deadline consumed → no duplicate re-fire waiting.
    assert sched._stats.pending_snooze_until is None


@pytest.mark.asyncio
async def test_snooze_refire_transient_error_keeps_pending(
    stats_path, monkeypatch,
) -> None:
    """A transient server error at the re-fire moment must NOT consume the
    snooze — the next tick retries (bounded by snooze_max_defer_secs)."""
    t0 = datetime(2026, 5, 4, 11, 5)
    # First a good response so the original fire + snooze succeed.
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, now=t0)
    clock = {"now": t0}
    sched._now_fn = lambda: clock["now"]  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    notifier.simulate_snooze()
    # Re-fire time arrives, but the server now returns a transient error.
    _patch_fetch(monkeypatch, TodaySessionResponse(status="transient_error"))
    clock["now"] = t0 + timedelta(hours=1, seconds=1)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_transient_error"
    # Snooze still armed → next tick retries.
    assert sched._stats.pending_snooze_until is not None


@pytest.mark.asyncio
async def test_first_fire_skipped_when_snooze_pending(
    stats_path, monkeypatch,
) -> None:
    """First-fire-on-start defers to the tick loop when a snooze is pending,
    rather than racing it via the ignore_gates path."""
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    now = datetime(2026, 5, 4, 14, 0)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, cfg=cfg, now=now)
    # Pending snooze due shortly; last_fire_at left None so cooldown alone
    # would NOT block first-fire — proving the snooze guard is what stops it.
    sched._stats.pending_snooze_until = (now + timedelta(minutes=30)).isoformat()
    sched._stats.last_fire_at = None
    await sched._maybe_first_fire()
    assert notifier.actionable_calls == []


def test_snooze_stale_pending_dropped_on_start(stats_path, monkeypatch) -> None:
    """A snooze whose window fully lapsed (agent off overnight) is cleared."""
    _patch_fetch(monkeypatch, _ok_response())
    now = datetime(2026, 5, 5, 11, 5)
    sched, _, _ = _make_scheduler(stats_path=stats_path, now=now)
    sched._stats.pending_snooze_until = (now - timedelta(hours=30)).isoformat()
    sched._maybe_clear_stale_snooze()
    assert sched._stats.pending_snooze_until is None
