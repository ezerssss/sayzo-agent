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
    ) -> bool:
        self.actionable_calls.append(
            dict(
                title=title,
                body=body,
                button_label=button_label,
                expire_after_secs=expire_after_secs,
            )
        )
        if not self.dispatch_returns:
            return False
        self._latch = False
        self._on_pressed = on_pressed
        self._on_expire = on_expire
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
        min_idle_secs=180.0,
        cold_start_hour=11,
        min_hour=9,
        lunch_start_hour=12,
        lunch_end_hour=13,
        max_hour=17,
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
    sched._started_at_mono = time.monotonic() - cfg.min_idle_secs - 60.0
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
async def test_skips_during_lunch(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, _, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 12, 30)
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_lunch"


@pytest.mark.asyncio
async def test_skips_before_min_hour(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, _, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 7, 0)
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_outside_window"


@pytest.mark.asyncio
async def test_skips_within_boot_warmup(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, _, _ = _make_scheduler(stats_path=stats_path)
    sched._started_at_mono = time.monotonic()  # just booted
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_boot_warmup"


@pytest.mark.asyncio
async def test_skips_on_weekend_with_no_history(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, _, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 9, 11, 5)  # Saturday
    )
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_weekend_no_history"


@pytest.mark.asyncio
async def test_fires_on_weekend_after_engagement(stats_path, monkeypatch) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 9, 11, 5)
    )
    # Seed a past Saturday tap so the weekend gate opens.
    past_sat = datetime(2026, 5, 2, 11, 0)
    sched._model.record_fire(past_sat)
    sched._model.record_outcome(past_sat, "tap", latency_ms=10000, session_id="x")
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
async def test_402_marks_day_done_no_tray_fallback(stats_path, monkeypatch) -> None:
    _patch_fetch(
        monkeypatch, TodaySessionResponse(status="over_credit")
    )
    sched, notifier, _ = _make_scheduler(stats_path=stats_path)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_over_credit"
    assert sched._stats.last_fired_on_day == "2026-05-04"
    assert sched._stats.eod_fallback_shown_on is None
    assert notifier.actionable_calls == []


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


@pytest.mark.asyncio
async def test_dispatch_failed_falls_back_to_eod(stats_path, monkeypatch) -> None:
    """Notifier returns False → mark day done + try EOD fallback."""
    _patch_fetch(monkeypatch, _ok_response())
    fn = FakeNotifier(dispatch_returns=False)
    sched, _, _ = _make_scheduler(stats_path=stats_path, fake_notifier=fn)
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_dispatch_failed"
    assert sched._stats.last_fired_on_day == "2026-05-04"


# ---------------------------------------------------------------------------
# EOD tray fallback
# ---------------------------------------------------------------------------


class FakeTrayState:
    def __init__(self) -> None:
        self.label: Optional[str] = None

    def set_eod_drill_label(self, label: Optional[str]) -> None:
        self.label = label

    def get_eod_drill_label(self) -> Optional[str]:
        return self.label


@pytest.mark.asyncio
async def test_eod_fallback_surfaces_when_window_passed_unfired(
    stats_path, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(eod_fallback_hour=17)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(
        stats_path=stats_path,
        cfg=cfg,
        now=datetime(2026, 5, 4, 18, 0),
    )
    sched._tray_state = tray  # type: ignore[assignment]
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_outside_window"
    assert tray.label == "Today's drill — 60s"
    assert sched._stats.eod_fallback_shown_on == "2026-05-04"


@pytest.mark.asyncio
async def test_eod_fallback_persisted_no_repeat(
    stats_path, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(eod_fallback_hour=17)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(
        stats_path=stats_path,
        cfg=cfg,
        now=datetime(2026, 5, 4, 18, 0),
    )
    sched._tray_state = tray  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert tray.label == "Today's drill — 60s"
    # Clear the tray label as if the user closed it; the scheduler must
    # NOT re-set it the same day.
    tray.label = None
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert tray.label is None


@pytest.mark.asyncio
async def test_eod_tray_click_opens_link_and_records_soft_tap(
    stats_path, monkeypatch
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    opened = _patch_open(monkeypatch)
    cfg = _cfg(eod_fallback_hour=17)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(
        stats_path=stats_path,
        cfg=cfg,
        now=datetime(2026, 5, 4, 18, 0),
    )
    sched._tray_state = tray  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    sched.on_eod_tray_click()
    assert opened == ["https://sayzo.app/drills/sess_test"]
    bucket = sched._model.stats.buckets["0-18"]
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
    # Reload with daily disabled; subsequent call (different day) skips.
    sched.reload_config(_cfg(daily_drill_enabled=False), master_enabled=True)
    sched._stats.last_fired_on_day = None  # simulate next-day reset
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
