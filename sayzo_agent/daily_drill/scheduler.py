"""DailyDrillScheduler — orchestrates the per-workday notification.

Lives as a single asyncio task spawned from ``Agent.run()`` next to the
arm controller. Ticks every ``tick_secs`` (default 60s) and runs a chain
of gates; if all pass and the bucket model wants to fire today, it calls
``/api/sessions/today`` and dispatches an actionable toast via
``notifier.notify_actionable``.

Outcome flow:

* ``on_tap`` (notifier callback) → opens ``deep_link_url`` in default
  browser, records ``tap`` (within ``dismiss_window_secs``) or
  ``soft_tap`` (within ``soft_tap_window_secs``); ignores anything later.
* ``on_expire`` (notifier watchdog) → records ``expire`` and saves stats.
* ``on_eod_tray_click`` → records a ``soft_tap`` for the EOD-fallback
  surface and opens the deep link.

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
from datetime import datetime
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
    "skipped_auth_error",
    "skipped_transient_error",
    "skipped_dispatch_failed",
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
_OS_DISABLED_LABEL = "⚠ Enable notifications to get drills"


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
        self._started_at_mono = time.monotonic()
        self._stop.clear()
        self._task = asyncio.create_task(self._tick_loop(), name="daily_drill")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("[daily_drill] stopped")

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

    # ------------------------------------------------------------------
    # Gate composition + fire
    # ------------------------------------------------------------------

    async def _evaluate_and_maybe_fire(self, *, ignore_gates: bool) -> FireResult:
        now = self._now_fn()
        today = now.date().isoformat()

        # Cross-midnight reset BEFORE the already-fired check so a new
        # day clears yesterday's state.
        if self._last_seen_day != today:
            self._reset_daily_state(today)
            self._last_seen_day = today

        if self._stats.last_fired_on_day == today and not ignore_gates:
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

        if self._has_auth_cached is False and not ignore_gates:
            self._maybe_show_os_disabled_warning()
            return FireResult(status="skipped_os_disabled")

        if not ignore_gates:
            # Weekend cold-start gate
            if now.weekday() >= 5 and not self._model.has_any_weekend_engagement():
                return FireResult(status="skipped_weekend_no_history")

            # Time-window gates
            if now.hour < self._cfg.min_hour:
                return FireResult(status="skipped_outside_window")
            if (
                self._cfg.lunch_start_hour
                <= now.hour
                < self._cfg.lunch_end_hour
            ):
                return FireResult(status="skipped_lunch")
            if now.hour >= self._cfg.eod_fallback_hour:
                await self._maybe_surface_eod_fallback(now, today)
                return FireResult(status="skipped_outside_window")

            # Boot warmup
            elapsed_since_start = time.monotonic() - self._started_at_mono
            if elapsed_since_start < self._cfg.min_idle_secs:
                return FireResult(status="skipped_boot_warmup")

            # Idle gate
            if self._get_idle_secs_fn() < self._cfg.min_idle_secs:
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

            # Model picks the hour
            chosen_hour = self._model.pick_hour_today(as_of=now, rng=self._rng)
            if chosen_hour is None:
                return FireResult(status="skipped_model_no_pick")
            if now.hour < chosen_hour:
                return FireResult(status="skipped_model_wants_later")

        return await self._fire_drill(now, today)

    async def _fire_drill(self, now: datetime, today: str) -> FireResult:
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
        if resp.status == "over_credit":
            # Mark "decided not to fire" so we don't keep hitting /today
            # all day when the user's credits are exhausted.
            self._stats.last_fired_on_day = today
            self._save_stats()
            return FireResult(status="skipped_over_credit", response=resp)
        if resp.status in ("transient_error", "unknown_error"):
            return FireResult(status="skipped_transient_error", response=resp)
        # ok / still_processing / retry_required → fire

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

        dispatched = self._notifier.notify_actionable(
            title=title,
            body=body,
            button_label="Open drill",
            on_pressed=lambda: self._on_pressed_callback(pending),
            expire_after_secs=self._cfg.dismiss_window_secs,
            on_expire=lambda: self._on_expire_callback(pending),
        )
        if not dispatched:
            # Backend can't render — fall back to the tray surface for
            # the day. Mark the day so we don't keep retrying.
            log.warning(
                "[daily_drill] notify_actionable returned False; "
                "falling back to EOD tray surface"
            )
            self._stats.last_fired_on_day = today
            await self._maybe_surface_eod_fallback(now, today)
            self._save_stats()
            return FireResult(
                status="skipped_dispatch_failed", response=resp, title=title, body=body,
            )

        self._stats.last_fired_on_day = today
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
        with self._lock:
            self._pending = None
        self._save_stats()
        log.info(
            "[daily_drill] recorded outcome=expire session=%s", pending.session_id
        )

    # ------------------------------------------------------------------
    # End-of-day tray fallback
    # ------------------------------------------------------------------

    async def _maybe_surface_eod_fallback(
        self, now: datetime, today: str
    ) -> None:
        """Surface a tray item if past EOD cutoff and not already shown."""
        if self._stats.eod_fallback_shown_on == today:
            return
        if self._stats.last_fired_on_day == today and self._current_today is None:
            # We already fired (real toast or marked decided); nothing to
            # surface unless the toast itself failed and set
            # _current_today separately.
            return

        # Best-effort fetch — if it fails, leave the tray quiet.
        if self._current_today is None:
            client = self._auth_client_factory()
            if client is None:
                return
            try:
                resp = await fetch_today_session(client)
            except Exception:
                log.debug(
                    "[daily_drill] EOD fetch failed", exc_info=True
                )
                return
            if not resp.fireable:
                return
            self._current_today = resp

        if self._tray_state is not None:
            try:
                self._tray_state.set_eod_drill_label(_EOD_LABEL)
            except Exception:
                log.warning(
                    "[daily_drill] failed to set tray EOD label", exc_info=True
                )
        self._stats.eod_fallback_shown_on = today
        self._save_stats()
        log.info("[daily_drill] EOD tray fallback surfaced for %s", today)

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
    # OS-disabled UX
    # ------------------------------------------------------------------

    def _maybe_show_os_disabled_warning(self) -> None:
        if self._stats.os_disabled_prompt_shown:
            return
        if self._tray_state is not None:
            try:
                self._tray_state.set_eod_drill_label(_OS_DISABLED_LABEL)
            except Exception:
                log.debug("[daily_drill] tray label set failed", exc_info=True)
        self._stats.os_disabled_prompt_shown = True
        self._save_stats()
        log.warning(
            "[daily_drill] OS notifications disabled — surfaced one-time prompt"
        )

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
        """Cross-midnight reset: clear today's pending fire + tray label."""
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
