"""DailyDrillScheduler — orchestrates the daily notification.

Lives as a single asyncio task spawned from ``Agent.run()`` next to the
arm controller. Ticks every ``tick_secs`` (default 60s) and runs a chain
of gates; if all pass it calls ``/api/sessions/today`` and dispatches an
actionable toast via ``notifier.notify_actionable``.

v3.6.7: timing is driven by **activity + cooldown**, not time-of-day:

* **Activity gate** — user must be idle ≥ ``min_idle_secs`` (don't
  interrupt mid-keystroke) AND ≤ ``max_idle_secs`` (proves the user is
  actually present, not asleep / away). One gate covers daytime workers,
  night-shift workers, evening workers, irregular schedules — no
  configuration needed.
* **Cooldown gate** — fires no more than once per ``cooldown_secs``
  (default 18h). A simple timestamp comparison; no calendar / shift /
  timezone logic. Replaces the v3.6.5 ``last_fired_on_day == today``
  check.

The bucket model still records outcomes (tap / soft_tap / expire per
hour-of-day) as telemetry but no longer gates firing — Thompson
sampling is dead weight in the activity+cooldown world.

Outcome flow:

* ``on_tap`` (notifier callback) → opens ``deep_link_url`` in default
  browser, records ``tap`` (within ``dismiss_window_secs``) or
  ``soft_tap`` (within ``soft_tap_window_secs``); ignores anything later.
* ``on_expire`` (notifier watchdog) → records ``expire`` and saves stats.
* ``on_eod_tray_click`` → records a ``soft_tap`` and opens the deep link
  (kept for any tray label leftover from a dispatch-failure case).

The scheduler holds a single in-memory ``_pending_fire`` representing
the in-flight notification. The notifier's single-fire latch guarantees
exactly one of ``on_tap`` / ``on_expire`` runs per fire.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

from .api import TodaySessionResponse, fetch_today_session
from .copy import compose_copy
from .model import BucketModel, NotificationStats

if TYPE_CHECKING:
    from ..arm.controller import ArmState
    from ..auth.client import AuthenticatedClient
    from ..auth.store import TokenStore
    from ..config import NotificationConfig
    from ..gui.tray import TrayState
    from ..notify import Notifier

log = logging.getLogger(__name__)


# Status codes returned from fire_now for the test-trigger CLI.
# v3.6.5: `skipped_weekend_no_history` and `skipped_over_credit` are
# retained as literal values to preserve forward/back-compat with any
# log-scraping or test helpers that pattern-match against them, but
# the scheduler no longer emits either: the weekend cold-start gate
# was removed (was silencing new users entirely on Sat/Sun) and the
# platform's `/sessions/today` endpoint never returns 402 (credit
# charge moved to `/sessions/complete`).
FireResultStatus = Literal[
    "fired",
    "skipped_master_disabled",
    "skipped_daily_disabled",
    "skipped_unauthenticated",
    "skipped_os_disabled",
    "skipped_already_fired",
    "skipped_armed",
    "skipped_mic_active",
    "skipped_idle",
    "skipped_outside_window",
    "skipped_lunch",
    "skipped_weekend_no_history",
    "skipped_boot_warmup",
    "skipped_model_no_pick",
    "skipped_model_wants_later",
    "skipped_in_progress",
    "skipped_over_credit",
    "skipped_no_deep_link",
    "skipped_auth_error",
    "skipped_transient_error",
    "skipped_dispatch_failed",
    # v3.8.x snooze: a re-fire is pending but not yet due (still waiting
    # out the snooze window), and the give-up status when the user was
    # busy past snooze_max_defer_secs so we fell back to the EOD tray.
    "skipped_snoozed",
    "skipped_snooze_gave_up",
]


@dataclass
class FireResult:
    """Returned by ``fire_now`` for the test-trigger CLI / callers that
    want to know what happened on a manual trigger."""

    status: FireResultStatus
    response: Optional[TodaySessionResponse] = None
    title: Optional[str] = None
    body: Optional[str] = None


@dataclass
class _PendingFire:
    fired_at_dt: datetime
    fired_at_mono: float
    session_id: Optional[str]
    deep_link_url: Optional[str]


# Tray label string used for the EOD fallback. Centralised so the unit
# tests can assert on it without re-spelling the copy.
_EOD_LABEL = "Today's drill — 60s"


class DailyDrillScheduler:
    """Per-workday daily-drill notification orchestrator.

    Constructed once per agent process. Inject all platform-dependent
    queries (idle, arm state, mic activity) so unit tests can drive
    the gate logic with frozen time and seeded random.
    """

    def __init__(
        self,
        cfg: "NotificationConfig",
        master_notifications_enabled: bool,
        stats_path: Path,
        notifier: "Notifier",
        token_store: "TokenStore",
        auth_client_factory: Callable[[], Optional["AuthenticatedClient"]],
        arm_state_fn: Callable[[], "ArmState"],
        is_mic_active_fn: Callable[[], bool],
        get_idle_secs_fn: Optional[Callable[[], float]] = None,
        tray_state: Optional["TrayState"] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        rng: Optional[random.Random] = None,
        tick_secs: Optional[float] = None,
    ) -> None:
        self._cfg = cfg
        self._master_enabled = master_notifications_enabled
        self._stats_path = stats_path
        self._notifier = notifier
        self._token_store = token_store
        self._auth_client_factory = auth_client_factory
        self._arm_state_fn = arm_state_fn
        self._is_mic_active_fn = is_mic_active_fn
        if get_idle_secs_fn is None:
            from ..idle import get_idle_seconds
            get_idle_secs_fn = get_idle_seconds
        self._get_idle_secs_fn = get_idle_secs_fn
        self._tray_state = tray_state
        self._now_fn = now_fn or (lambda: datetime.now())
        self._rng = rng or random.Random()
        self._tick_secs = tick_secs if tick_secs is not None else cfg.tick_secs

        self._model = BucketModel.load(stats_path, cfg)
        # Cached value of the OS-level "notifications enabled" probe. Set
        # once at start(); rechecked on reload_config.
        self._has_auth_cached: Optional[bool] = None
        # Tracks whether we've already logged the "no tokens" transition
        # so we don't spam the log every tick.
        self._auth_warned: bool = False

        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._started_at_mono: float = 0.0

        # In-flight fire state. Lock guards mutation across the asyncio
        # loop tick + notifier-callback threads.
        self._lock = threading.Lock()
        self._pending: Optional[_PendingFire] = None
        self._current_today: Optional[TodaySessionResponse] = None
        self._fetch_in_flight: bool = False
        self._last_seen_day: Optional[str] = None
        # Tracks the previous tick's skip-reason so we can log INFO only on
        # transitions instead of every 60s — DEBUG is fine for the steady
        # state, but the first time a gate flips silently we want a single
        # INFO line so future "I never see notifications" reports are
        # diagnosable from agent.log without flipping log level.
        self._last_skip_status: Optional[FireResultStatus] = None
        # One-shot first-fire task (see _maybe_first_fire). Held so stop()
        # can cancel it cleanly during shutdown.
        self._first_fire_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info(
            "[daily_drill] starting: enabled=%s daily=%s tick=%ss",
            self._master_enabled,
            self._cfg.daily_drill_enabled,
            self._tick_secs,
        )
        self._has_auth_cached = self._notifier.has_authorisation_sync()
        log.info("[daily_drill] OS notification authorisation: %s", self._has_auth_cached)
        self._maybe_clear_stale_snooze()
        self._started_at_mono = time.monotonic()
        self._stop.clear()
        self._task = asyncio.create_task(self._tick_loop(), name="daily_drill")
        # v3.6.5: guaranteed first-fire after boot warmup so a user who
        # launches the agent at noon and quits at 5pm always sees one
        # toast that day, even if Thompson sampling would otherwise pick
        # a later hour. The tick loop still runs all day for additional
        # learning fires when the user keeps the agent open.
        self._first_fire_task = asyncio.create_task(
            self._maybe_first_fire(), name="daily_drill_first_fire",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._first_fire_task is not None:
            self._first_fire_task.cancel()
            try:
                await self._first_fire_task
            except (asyncio.CancelledError, Exception):
                pass
            self._first_fire_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("[daily_drill] stopped")

    async def _maybe_first_fire(self) -> None:
        """One-shot guaranteed fire after boot warmup.

        Smart timing is great when the agent runs all day — but a user
        who launches Sayzo at noon and quits at 5pm shouldn't depend on
        Thompson sampling lining up with their actual window. Sleep
        through boot warmup, then fire if we haven't already.

        Gates honored: auth (via _evaluate's token check), armed, and
        mic-active. Timing gates (idle, time-of-day window, Thompson)
        are bypassed via ignore_gates=True so a noon launch still fires.

        The armed/mic checks must happen BEFORE the ignore_gates=True
        call because that flag bypasses the inside-the-gate-chain armed/
        mic checks too. We never want a daily-drill toast to fire mid-
        meeting — that's a "ringing in your boss's Zoom" UX disaster.

        Cancellable mid-warmup via stop(): wait_for-stop pattern wakes
        the task on shutdown so we don't fire a stray toast at the
        exact moment the agent is exiting.
        """
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=self._cfg.boot_warmup_secs + 5.0,
            )
            return  # stop() was called during warmup
        except asyncio.TimeoutError:
            pass

        now = self._now_fn()
        if self._is_in_cooldown(now):
            log.info(
                "[daily_drill] first-fire on start: within cooldown; skipping"
            )
            return
        # Defer to the tick loop when a snooze re-fire is pending. Today the
        # cooldown check above already covers this (a non-stale snooze keeps
        # last_fire_at recent, well inside cooldown_secs) — but that's a
        # magnitude coincidence (snooze_max_defer_secs < cooldown_secs). This
        # explicit guard keeps first-fire from racing the tick loop's re-fire
        # even if those knobs are retuned. (Stale snoozes were already cleared
        # by _maybe_clear_stale_snooze in start().)
        if self._stats.pending_snooze_until:
            log.info(
                "[daily_drill] first-fire on start: snooze re-fire pending; "
                "leaving it to the tick loop"
            )
            return

        # Defend against firing during an active meeting. ignore_gates=True
        # below would otherwise bypass these checks (they live inside
        # the if-not-ignore_gates block in _evaluate_and_maybe_fire).
        try:
            from ..arm.controller import ArmState
            if self._arm_state_fn() == ArmState.ARMED:
                log.info("[daily_drill] first-fire skipped: agent armed")
                return
        except Exception:
            log.debug(
                "[daily_drill] first-fire arm-state probe failed", exc_info=True,
            )
        try:
            if self._is_mic_active_fn():
                log.info("[daily_drill] first-fire skipped: mic active")
                return
        except Exception:
            log.debug(
                "[daily_drill] first-fire mic-active probe failed", exc_info=True,
            )

        log.info("[daily_drill] first-fire on start: attempting")
        try:
            result = await self._evaluate_and_maybe_fire(ignore_gates=True)
        except Exception:
            log.warning(
                "[daily_drill] first-fire raised", exc_info=True
            )
            return
        log.info("[daily_drill] first-fire result: %s", result.status)

    def reload_config(
        self, cfg: "NotificationConfig", master_enabled: bool,
    ) -> None:
        log.info(
            "[daily_drill] reload_config: enabled=%s daily=%s tick=%ss",
            master_enabled,
            cfg.daily_drill_enabled,
            cfg.tick_secs,
        )
        self._cfg = cfg
        self._master_enabled = master_enabled
        # Re-probe OS authorisation so a user who just toggled
        # notifications on in System Settings gets picked up.
        self._has_auth_cached = self._notifier.has_authorisation_sync()
        # tick_secs change applies on the NEXT tick because the running
        # loop is awaiting on the old value; close enough for our 60s
        # cadence (a few minutes of drift is fine for a daily nudge).

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        log.info("[daily_drill] tick loop started")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick_once()
                except Exception:
                    log.warning("[daily_drill] tick raised", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._tick_secs)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            log.info("[daily_drill] tick loop exited")

    async def _tick_once(self) -> None:
        """Re-evaluate gates and maybe fire."""
        result = await self._evaluate_and_maybe_fire(ignore_gates=False)
        log.debug("[daily_drill] tick result: %s", result.status)
        if result.status != self._last_skip_status and result.status != "fired":
            log.info(
                "[daily_drill] skip-reason changed: %s → %s",
                self._last_skip_status,
                result.status,
            )
        self._last_skip_status = result.status

    # ------------------------------------------------------------------
    # Gate composition + fire
    # ------------------------------------------------------------------

    async def _evaluate_and_maybe_fire(self, *, ignore_gates: bool) -> FireResult:
        now = self._now_fn()
        today = now.date().isoformat()

        # Calendar-day reset for `eod_fallback_shown_on` and pending state
        # only — the once-per-day fire gate was replaced by the timestamp
        # cooldown (see _is_in_cooldown) in v3.6.7.
        if self._last_seen_day != today:
            self._reset_daily_state(today)
            self._last_seen_day = today

        # Snooze re-fire (v3.8.x). If the user clicked "Snooze 1h" earlier,
        # a re-fire deadline is stamped in stats. Until it passes we stay
        # quiet; once it passes we bypass the cooldown gate below (the user
        # explicitly asked to be re-pinged) but still honor every activity
        # / meeting gate. If they're busy at the deadline we keep retrying
        # each tick; past snooze_max_defer_secs beyond it we give up to the
        # quiet EOD tray so a back-to-back-meetings afternoon can't defer
        # forever. ignore_gates (manual fire-now) skips all of this and
        # fires immediately — but first-fire-on-start is naturally blocked
        # by the original fire's cooldown, so it won't double-fire.
        snooze_bypass_cooldown = False
        if not ignore_gates and self._stats.pending_snooze_until:
            snooze_due = self._parse_snooze_until()
            if snooze_due is None:
                self._clear_pending_snooze()
                self._save_stats()
            elif now < snooze_due:
                return FireResult(status="skipped_snoozed")
            elif (now - snooze_due).total_seconds() > self._cfg.snooze_max_defer_secs:
                log.info(
                    "[daily_drill] snooze re-fire abandoned after %.0fs "
                    "unreached (user away or busy past the window); falling "
                    "back to EOD tray",
                    (now - snooze_due).total_seconds(),
                )
                self._clear_pending_snooze()
                # Only surface the tray label when we still hold the
                # response to open on click. After a restart mid-snooze
                # _current_today is None; a label that no-ops on click is
                # worse than no label (today's normal fire still covers it).
                if self._current_today is not None:
                    self._set_tray_eod_label_if_needed(today)
                self._save_stats()
                return FireResult(status="skipped_snooze_gave_up")
            else:
                snooze_bypass_cooldown = True

        # v3.6.7: cooldown replaces `last_fired_on_day == today`. A single
        # timestamp comparison handles every schedule (daytime, night
        # shift, irregular) without time-of-day knowledge. ignore_gates
        # bypasses this for the test-trigger CLI; first-fire-on-start
        # honors it via its own _is_in_cooldown call. snooze_bypass_cooldown
        # lets a due snooze re-fire through despite the original fire's
        # cooldown still being active.
        if (
            not ignore_gates
            and not snooze_bypass_cooldown
            and self._is_in_cooldown(now)
        ):
            return FireResult(status="skipped_already_fired")

        if not self._master_enabled and not ignore_gates:
            return FireResult(status="skipped_master_disabled")
        if not self._cfg.daily_drill_enabled and not ignore_gates:
            return FireResult(status="skipped_daily_disabled")

        if not self._token_store.has_tokens():
            if not self._auth_warned:
                log.info("[daily_drill] no tokens — skipping until login")
                self._auth_warned = True
            return FireResult(status="skipped_unauthenticated")
        # Reset the warn-once latch when the user signs in.
        self._auth_warned = False

        # Pre-v2.10 this branch surfaced a one-time tray warning when
        # the OS had silently denied notification permissions (the
        # `[notify] auth probe: has_authorisation=False` regression).
        # With v2.10+ the HUD owns its own UI — `has_authorisation_sync`
        # returns True whenever the launcher is alive — so this branch
        # is unreachable and has been removed. The `skipped_os_disabled`
        # status name is retained in `_FIRE_STATUSES` for backward
        # compat with on-disk stats from older versions.

        if not ignore_gates:
            # Boot warmup — agent must have been alive for boot_warmup_secs
            # before we'll fire. Catches the "agent just launched, give it
            # a moment to settle" case.
            elapsed_since_start = time.monotonic() - self._started_at_mono
            if elapsed_since_start < self._cfg.boot_warmup_secs:
                return FireResult(status="skipped_boot_warmup")

            # Activity gate (v3.6.7): recency CAP, not a minimum. The user
            # must have touched the keyboard/mouse within max_idle_secs
            # (default 10min) AND not be actively typing right now
            # (min_idle_secs, default 30s). This single gate replaces the
            # entire time-of-day window: a sleeping user is idle for hours
            # (far above the cap → skipped), a working user is idle
            # 30s-few-minutes between keystrokes (in the window → fires).
            # Works identically for daytime workers, night-shift workers,
            # evening workers, weekend warriors — no schedule
            # configuration needed.
            idle_secs = self._get_idle_secs_fn()
            if idle_secs < self._cfg.min_idle_secs:
                return FireResult(status="skipped_idle")
            if idle_secs > self._cfg.max_idle_secs:
                return FireResult(status="skipped_idle")

            # Meeting gate — armed agent OR raw mic-in-use
            try:
                from ..arm.controller import ArmState
                if self._arm_state_fn() == ArmState.ARMED:
                    return FireResult(status="skipped_armed")
            except Exception:
                log.debug("[daily_drill] arm state probe failed", exc_info=True)
            try:
                if self._is_mic_active_fn():
                    return FireResult(status="skipped_mic_active")
            except Exception:
                log.debug("[daily_drill] mic active probe failed", exc_info=True)

            # Thompson sampling is now telemetry-only (v3.6.7). With
            # activity + cooldown driving timing, "pick the best hour"
            # is meaningless — the answer is always "the next moment the
            # user is present post-cooldown." We still record outcomes
            # so a future UI could surface "your peak engagement hours"
            # without re-instrumenting.

        result = await self._fire_drill(
            now, today, is_snooze_refire=snooze_bypass_cooldown,
        )
        # Any fire that actually reaches the user consumes a pending snooze —
        # both the snooze-bypass re-fire AND a manual fire_now / first-fire
        # (ignore_gates) that dispatched while a snooze was still armed.
        # Without this the leftover deadline would re-fire a duplicate toast
        # at the original re-fire time. A transient failure (no dispatch)
        # leaves the deadline set so the next tick retries — bounded by
        # snooze_max_defer_secs.
        if (
            self._stats.pending_snooze_until is not None
            and result.status in ("fired", "skipped_dispatch_failed")
        ):
            self._stats.pending_snooze_until = None
            self._save_stats()
        return result

    # ------------------------------------------------------------------
    # Snooze helpers (v3.8.x)
    # ------------------------------------------------------------------

    def _parse_snooze_until(self) -> Optional[datetime]:
        """Parse the pending re-fire timestamp; None if unset / corrupt."""
        raw = self._stats.pending_snooze_until
        if not raw:
            return None
        # `or None` in from_dict only filters falsy values, so a hand-corrupted
        # stats file could leave a truthy non-string here — fromisoformat
        # raises TypeError (not ValueError) on that. Catch both so a bad
        # on-disk value degrades to "no snooze" instead of crashing start().
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            log.warning(
                "[daily_drill] unparseable pending_snooze_until=%r; clearing", raw
            )
            return None

    def _clear_pending_snooze(self) -> None:
        """Drop the snooze re-fire deadline (caller is responsible for save)."""
        self._stats.pending_snooze_until = None

    def _maybe_clear_stale_snooze(self) -> None:
        """On start, discard a snooze whose re-fire window has fully lapsed.

        If the agent was off long enough that the re-fire deadline plus the
        max-defer grace has passed, the snooze is dead — clear it so we
        don't surface a stale, possibly day-old drill toast on next tick.
        """
        if not self._stats.pending_snooze_until:
            return
        due = self._parse_snooze_until()
        if due is None:
            self._clear_pending_snooze()
            self._save_stats()
            return
        if (self._now_fn() - due).total_seconds() > self._cfg.snooze_max_defer_secs:
            log.info(
                "[daily_drill] clearing stale pending snooze (was due %s)",
                self._stats.pending_snooze_until,
            )
            self._clear_pending_snooze()
            self._save_stats()

    def _is_in_cooldown(self, now: datetime) -> bool:
        """True if a fire happened within the last cooldown_secs.

        Replaces the v3.6.5 `last_fired_on_day == today` calendar check
        with a simple timestamp comparison. Works in any timezone, across
        any schedule, no shift_today / date arithmetic.
        """
        if not self._stats.last_fire_at:
            return False
        try:
            last = datetime.fromisoformat(self._stats.last_fire_at)
        except ValueError:
            # Corrupt or pre-v3.6.7 stats file with no last_fire_at.
            # Treat as never-fired so we don't lock out the user forever
            # on a one-off parse error.
            return False
        secs_since = (now - last).total_seconds()
        return secs_since < self._cfg.cooldown_secs

    async def _fire_drill(
        self, now: datetime, today: str, *, is_snooze_refire: bool = False,
    ) -> FireResult:
        with self._lock:
            if self._fetch_in_flight:
                return FireResult(status="skipped_in_progress")
            self._fetch_in_flight = True
        try:
            client = self._auth_client_factory()
            if client is None:
                return FireResult(status="skipped_unauthenticated")
            resp = await fetch_today_session(client)
        finally:
            with self._lock:
                self._fetch_in_flight = False

        if resp.status == "auth_required":
            return FireResult(status="skipped_auth_error", response=resp)
        # NOTE: `over_credit` (402) is no longer reachable — confirmed with
        # the platform team 2026-05-20 that `/sessions/today` never returns
        # 402; the credit charge moved to `/sessions/complete`. The branch
        # was deleted in v3.6.5; the api.py mapping no longer emits it.
        if resp.status in ("transient_error", "unknown_error"):
            return FireResult(status="skipped_transient_error", response=resp)
        if not resp.fireable:
            # Defensive: any future non-fireable status (or a defensive
            # over_credit slipping through) must NOT dispatch. Treat as
            # transient so the next tick re-fetches rather than locking
            # the day.
            log.warning(
                "[daily_drill] unexpected non-fireable status: %s; skipping",
                resp.status,
            )
            return FireResult(status="skipped_transient_error", response=resp)
        # ok / still_processing / retry_required → fire

        # 409 DRILL_STILL_PROCESSING is `fireable` but the platform omits
        # `deepLinkUrl` on that branch (server route.ts:66-76). Firing a
        # toast that can't be clicked is worse than skipping — the user
        # presses "Open drill" and nothing happens, then concludes the
        # whole feature is broken. Wait one tick; by then the server has
        # finished processing and the next response will carry a link.
        if not resp.deep_link_url:
            log.info(
                "[daily_drill] response is fireable but deep_link_url is None "
                "(likely 409 DRILL_STILL_PROCESSING); skipping until next tick"
            )
            return FireResult(status="skipped_no_deep_link", response=resp)

        self._current_today = resp
        title, body = compose_copy(resp)
        fired_at = self._now_fn()
        fired_at_mono = time.monotonic()
        pending = _PendingFire(
            fired_at_dt=fired_at,
            fired_at_mono=fired_at_mono,
            session_id=resp.session_id,
            deep_link_url=resp.deep_link_url,
        )
        with self._lock:
            self._pending = pending

        # Offer the "Snooze 1h" secondary button on the original fire only.
        # The re-fire (this is the one snooze the user already took) carries
        # no snooze button, so a drill can't be chained into "never fires"
        # (the v3.6.7 lesson).
        offer_snooze = not is_snooze_refire
        secondary_label = self._cfg.snooze_secondary_label if offer_snooze else None
        on_secondary = (
            (lambda: self._on_snoozed_callback(pending)) if offer_snooze else None
        )

        dispatched = self._notifier.notify_actionable(
            title=title,
            body=body,
            button_label="Open drill",
            on_pressed=lambda: self._on_pressed_callback(pending),
            expire_after_secs=self._cfg.dismiss_window_secs,
            on_expire=lambda: self._on_expire_callback(pending),
            secondary_button_label=secondary_label,
            on_secondary_pressed=on_secondary,
        )
        if not dispatched:
            # Backend can't render — fall back to the tray surface for
            # the day. Mark cooldown so we don't keep retrying every tick.
            # v3.6.7: timestamp cooldown replaces last_fired_on_day; the
            # tray label still dedupes by calendar day to prevent spam if
            # dispatch keeps failing.
            log.warning(
                "[daily_drill] notify_actionable returned False; "
                "falling back to EOD tray surface"
            )
            self._stats.last_fire_at = fired_at.isoformat()
            self._stats.last_fired_on_day = today  # legacy field, still set for telemetry
            self._set_tray_eod_label_if_needed(today)
            self._save_stats()
            return FireResult(
                status="skipped_dispatch_failed", response=resp, title=title, body=body,
            )

        self._stats.last_fire_at = fired_at.isoformat()
        self._stats.last_fired_on_day = today  # legacy field, still set for telemetry
        self._model.record_fire(fired_at)
        self._save_stats()
        log.info(
            "[daily_drill] fired: title=%r session=%s replay=%s",
            title,
            resp.session_id,
            resp.is_replay,
        )
        return FireResult(status="fired", response=resp, title=title, body=body)

    # ------------------------------------------------------------------
    # Outcome callbacks (called from notifier loop-thread)
    # ------------------------------------------------------------------

    def _on_pressed_callback(self, pending: _PendingFire) -> None:
        """Notifier on_pressed → record outcome + open browser."""
        # Snapshot _now() / monotonic delta so we classify tap vs soft_tap
        # consistently regardless of clock skew.
        now_dt = self._now_fn()
        elapsed_secs = max(0.0, time.monotonic() - pending.fired_at_mono)
        latency_ms = int(elapsed_secs * 1000)
        if elapsed_secs <= self._cfg.dismiss_window_secs:
            outcome = "tap"
        elif elapsed_secs <= self._cfg.soft_tap_window_secs:
            outcome = "soft_tap"
        else:
            log.info(
                "[daily_drill] tap dropped — too late (elapsed=%.1fs)", elapsed_secs
            )
            return
        if pending.deep_link_url:
            try:
                webbrowser.open(pending.deep_link_url)
            except Exception:
                log.warning(
                    "[daily_drill] webbrowser.open failed for %r",
                    pending.deep_link_url,
                    exc_info=True,
                )
        self._model.record_outcome(
            pending.fired_at_dt,
            outcome,
            latency_ms=latency_ms,
            session_id=pending.session_id,
        )
        # Terminal outcome — the drill is done; drop any snooze state so a
        # leftover deadline can't re-fire after the user already engaged.
        self._clear_pending_snooze()
        with self._lock:
            self._pending = None
        self._save_stats()
        log.info(
            "[daily_drill] recorded outcome=%s latency_ms=%d session=%s",
            outcome,
            latency_ms,
            pending.session_id,
        )

    def _on_expire_callback(self, pending: _PendingFire) -> None:
        """Notifier on_expire → record `expire` outcome."""
        self._model.record_outcome(
            pending.fired_at_dt,
            "expire",
            latency_ms=None,
            session_id=pending.session_id,
        )
        # Terminal outcome — clear snooze state (see _on_pressed_callback).
        self._clear_pending_snooze()
        with self._lock:
            self._pending = None
        self._save_stats()
        log.info(
            "[daily_drill] recorded outcome=expire session=%s", pending.session_id
        )

    def _on_snoozed_callback(self, pending: _PendingFire) -> None:
        """Notifier on_secondary_pressed → record `snooze` + stamp re-fire.

        The user clicked "Snooze 1h". Record the deferral as its own
        outcome (the strongest "saw it, chose to wait" signal we have),
        then stamp the wall-clock re-fire time into stats so the tick loop
        re-fires after ``snooze_duration_secs``. ``last_fire_at`` is
        deliberately NOT touched — it still reflects the original toast, so
        first-fire-on-start stays blocked by the cooldown while the tick
        loop owns the re-fire.
        """
        now = self._now_fn()
        self._model.record_outcome(
            pending.fired_at_dt,
            "snooze",
            latency_ms=None,
            session_id=pending.session_id,
        )
        self._stats.pending_snooze_until = (
            now + timedelta(seconds=self._cfg.snooze_duration_secs)
        ).isoformat()
        with self._lock:
            self._pending = None
        self._save_stats()
        log.info(
            "[daily_drill] snooze: re-fire scheduled for %s session=%s",
            self._stats.pending_snooze_until,
            pending.session_id,
        )

    # ------------------------------------------------------------------
    # Tray fallback (last resort when notify_actionable returns False)
    # ------------------------------------------------------------------
    #
    # v3.6.7 removed the time-of-day EOD trigger. The tray label only
    # surfaces now when the HUD's notify_actionable fails outright (HUD
    # subprocess crashed, etc.). on_eod_tray_click below handles the
    # user's click on that fallback label.

    def _set_tray_eod_label_if_needed(self, today: str) -> None:
        """Set the EOD tray label, marking the day to avoid re-surfacing.

        Last-resort fallback used by both the EOD path (when toast
        dispatch failed) and the in-hours dispatch-failure path in
        _fire_drill. Kept synchronous because it doesn't await anything.
        """
        if self._stats.eod_fallback_shown_on == today:
            return
        if self._tray_state is not None:
            try:
                self._tray_state.set_eod_drill_label(_EOD_LABEL)
            except Exception:
                log.warning(
                    "[daily_drill] failed to set tray EOD label", exc_info=True
                )
        self._stats.eod_fallback_shown_on = today
        self._save_stats()
        log.warning(
            "[daily_drill] EOD toast dispatch failed; fell back to tray label"
        )

    def on_eod_tray_click(self) -> None:
        """User clicked the EOD tray item — open drill, record soft_tap."""
        resp = self._current_today
        if resp is None:
            log.info("[daily_drill] EOD tray click with no current_today")
            return
        if resp.deep_link_url:
            try:
                webbrowser.open(resp.deep_link_url)
            except Exception:
                log.warning(
                    "[daily_drill] EOD webbrowser.open failed", exc_info=True
                )
        # Record against the bucket of when the user actually clicked —
        # that's the real engagement signal.
        now = self._now_fn()
        self._model.record_fire(now)
        self._model.record_outcome(
            now, "soft_tap", latency_ms=None, session_id=resp.session_id,
        )
        if self._tray_state is not None:
            try:
                self._tray_state.set_eod_drill_label(None)
            except Exception:
                pass
        self._save_stats()
        log.info("[daily_drill] EOD tray clicked → soft_tap recorded")

    # ------------------------------------------------------------------
    # Manual trigger (CLI / debug menu)
    # ------------------------------------------------------------------

    async def fire_now(self, *, ignore_gates: bool = True) -> FireResult:
        """Bypass gates and fire (or report why we didn't).

        Used by the ``test-drill-notification`` CLI subcommand and the
        tray Debug submenu. Always re-runs the API fetch + dispatch path.
        """
        return await self._evaluate_and_maybe_fire(ignore_gates=ignore_gates)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @property
    def _stats(self) -> "NotificationStats":
        return self._model.stats

    def _save_stats(self) -> None:
        try:
            self._model.save_atomic(self._stats_path)
        except Exception:
            log.warning("[daily_drill] save_stats failed", exc_info=True)

    def _reset_daily_state(self, today: str) -> None:
        """Cross-midnight reset: clear today's pending fire + tray label.

        Note: pending *snooze* state is deliberately NOT cleared here.
        This runs on the first evaluation after construction too (when
        ``_last_seen_day`` is still None), so clearing here would nuke a
        snooze just loaded from disk on restart. Stale snoozes are instead
        dropped by ``_maybe_clear_stale_snooze`` on start, by the give-up
        branch once past ``snooze_max_defer_secs``, and naturally gated by
        the idle check when the user is away.
        """
        with self._lock:
            self._pending = None
            self._current_today = None
            self._fetch_in_flight = False
        if (
            self._tray_state is not None
            and self._stats.eod_fallback_shown_on != today
        ):
            try:
                # Only clear if a previous day's label is still showing.
                self._tray_state.set_eod_drill_label(None)
            except Exception:
                pass

    @property
    def eod_fallback_label(self) -> Optional[str]:
        """Read the current EOD label (for tests / tray bridge)."""
        if self._tray_state is None:
            return None
        try:
            return self._tray_state.get_eod_drill_label()
        except Exception:
            return None


__all__ = ["DailyDrillScheduler", "FireResult", "FireResultStatus"]
