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
        min_idle_secs=30.0,
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
async def test_eod_path_fires_real_toast_not_tray(
    stats_path, monkeypatch,
) -> None:
    """After max_hour the EOD path fires a real actionable toast.

    Pre-v3.6.5 this was tray-only; users don't watch the tray so the
    feature was effectively invisible after work hours.
    """
    _patch_fetch(monkeypatch, _ok_response())
    tray = FakeTrayState()
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path,
        now=datetime(2026, 5, 4, 20, 30),  # past max_hour=20
    )
    sched._tray_state = tray  # type: ignore[assignment]
    res = await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert res.status == "skipped_outside_window"
    # Real toast fired (NOT a silent tray-only update).
    assert len(notifier.actionable_calls) == 1
    assert sched._stats.last_fired_on_day == "2026-05-04"
    assert sched._stats.eod_fallback_shown_on == "2026-05-04"
    # Tray label remains None — toast dispatch succeeded so the
    # last-resort tray path was never reached.
    assert tray.label is None


@pytest.mark.asyncio
async def test_eod_path_falls_back_to_tray_when_dispatch_fails(
    stats_path, monkeypatch,
) -> None:
    """If notify_actionable returns False (HUD unavailable), the EOD
    path falls back to the tray label as a genuine last-resort."""
    _patch_fetch(monkeypatch, _ok_response())
    fn = FakeNotifier(dispatch_returns=False)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(
        stats_path=stats_path,
        fake_notifier=fn,
        now=datetime(2026, 5, 4, 20, 30),
    )
    sched._tray_state = tray  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert tray.label == "Today's drill — 60s"
    assert sched._stats.eod_fallback_shown_on == "2026-05-04"


@pytest.mark.asyncio
async def test_eod_does_not_fire_twice_in_one_day(
    stats_path, monkeypatch,
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    sched, notifier, _ = _make_scheduler(
        stats_path=stats_path, now=datetime(2026, 5, 4, 20, 30),
    )
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert len(notifier.actionable_calls) == 1
    # Second tick same day must NOT fire a second toast.
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    assert len(notifier.actionable_calls) == 1


@pytest.mark.asyncio
async def test_eod_tray_click_opens_link_and_records_soft_tap(
    stats_path, monkeypatch,
) -> None:
    """When the toast dispatch fails and falls back to the tray, the
    tray-click path still opens the drill + records soft_tap."""
    _patch_fetch(monkeypatch, _ok_response())
    opened = _patch_open(monkeypatch)
    fn = FakeNotifier(dispatch_returns=False)
    tray = FakeTrayState()
    sched, _, _ = _make_scheduler(
        stats_path=stats_path,
        fake_notifier=fn,
        now=datetime(2026, 5, 4, 20, 30),
    )
    sched._tray_state = tray  # type: ignore[assignment]
    await sched._evaluate_and_maybe_fire(ignore_gates=False)
    sched.on_eod_tray_click()
    assert opened == ["https://sayzo.app/drills/sess_test"]
    bucket = sched._model.stats.buckets["0-20"]
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
async def test_first_fire_on_start_skipped_if_already_fired_today(
    stats_path, monkeypatch,
) -> None:
    _patch_fetch(monkeypatch, _ok_response())
    cfg = _cfg(boot_warmup_secs=0.05)
    sched, notifier, _ = _make_scheduler(stats_path=stats_path, cfg=cfg)
    # Pretend the tick loop already fired earlier.
    sched._stats.last_fired_on_day = "2026-05-04"
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
