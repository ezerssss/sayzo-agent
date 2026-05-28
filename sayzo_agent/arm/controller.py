"""ArmController — the armed-only capture state machine.

Owns the decision of whether the agent is currently recording. Orchestrates:

- **Capture stream lifecycle**: ``mic.start()`` / ``sys.start()`` on arm;
  ``mic.stop()`` / ``sys.stop()`` on disarm. When disarmed, the OS-level
  mic indicator is off — the process has no open audio streams.
- **Hotkey confirmations**: start-confirmation toast before arming via
  hotkey, stop-confirmation before disarming. Double-tap within 1 s
  bypasses the confirmation.
- **Whitelist auto-suggest**: polls for a whitelisted meeting app holding
  the mic while disarmed; fires a consent toast; arms on user "Start
  coaching". Per-app cooldown after decline / session end.
- **PENDING_CLOSE handling**: subscribes to the detector's
  ``on_pending_close`` hook; shows the end-confirmation toast; commits
  close + disarms on "Yes, done" / timeout, reverts on "Not yet".
- **Long-meeting check-ins**: at elapsed marks (1h / 2h / 2h30 / ...),
  fires "Still in the meeting?" toast. "Wrap up" → disarm with reason
  CHECKIN_WRAP_UP.
- **Meeting-ended watcher** (whitelist-armed only): polls for the arm-app
  dropping the mic. 15 s grace → toast. "Keep going" snoozes 10 min; on
  snooze expiry, re-fires if still absent. Non-response on any toast
  defaults to Wrap up and commits the close.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, Optional

from ..account import AccountGateDecision
from ..config import ArmConfig, DetectorSpec
from ..conversation import ConversationDetector, SessionState
from ..models import SessionCloseReason
from ..notify import ConsentResult, Notifier
from . import detectors as _d
from . import seen_apps as _seen_apps
from .detectors import ForegroundInfo, MicState
from .hotkey import HotkeySource


ArmSource = Literal["hotkey", "whitelist"]
AccountGateFn = Callable[[], AccountGateDecision]

log = logging.getLogger(__name__)


# Small fast-path window for double-tapping the hotkey to bypass confirmation.
_DOUBLE_TAP_SECS = 1.0

# Time throttle for the diagnostic "[arm] no whitelist match" log. Layered
# on top of the existing signature-based dedup so background dictation
# apps that flicker the mic holders can't generate hundreds of log lines
# per hour by defeating the signature.
_NO_MATCH_LOG_THROTTLE_SECS = 30.0


class ArmState(str, Enum):
    DISARMED = "disarmed"
    ARMED = "armed"


@dataclass
class ArmReason:
    """Records why the agent is currently ARMED.

    - ``source`` is ``"hotkey"`` when the user pressed the hotkey, or
      ``"whitelist"`` when a consent toast from the auto-suggest path was
      accepted.
    - ``app_key`` and ``display_name`` are populated only for whitelist
      arms; they drive the meeting-ended watcher and the cooldown bucket.
    - ``target_pids`` is threaded into ``SystemCapture.start`` so the
      system-audio capture scopes to just those processes (WASAPI process
      loopback on Windows, CoreAudio Process Tap include-list on macOS).
      Empty tuple ⇒ endpoint-wide capture (today's behavior / fallback).
    - ``mic_device`` is the OS-level capture device the matched meeting
      app is using. Threaded into ``MicCapture.start`` so we record from
      the same mic the user is actually talking into. ``None`` ⇒ fall
      back to the OS default input device (today's behavior). The
      scope resolvers compute this from the holders surfaced by
      :mod:`platform_win` / :mod:`platform_mac`.
    """

    source: ArmSource
    app_key: Optional[str] = None
    display_name: Optional[str] = None
    target_pids: tuple[int, ...] = ()
    mic_device: Optional[str] = None


@dataclass
class _Cooldowns:
    """Per-app suppression state for the whitelist watcher.

    Single mechanism, keyed by ``app_key``: when the user declines a
    consent toast OR a whitelist-armed session ends, we mark the app as
    suppressed and clear it once the app releases the mic for
    ``decline_release_grace_secs`` continuous seconds. Leaving + rejoining
    a meeting (or closing + reopening a voice-mode session) counts as a
    new session, so a fresh prompt fires.

    A flat wall-clock cooldown was tried previously for the post-session
    case but caused user-reported bug 2026-04-28: stop a ChatGPT voice
    session, close it, reopen it within 10 min → no toast. Using the
    release-based mechanism for both paths means the watcher re-prompts
    as soon as the app actually re-acquires the mic.
    """
    # Value = monotonic time when the current "not holding" streak started,
    # or None if the app is currently still holding the mic (streak hasn't
    # started yet). Presence of the key = suppressed state.
    declined_release_at: dict[str, Optional[float]] = field(default_factory=dict)

    def active(self, app_key: str) -> bool:
        return app_key in self.declined_release_at

    def suppressed_keys(self) -> frozenset[str]:
        """Set of currently-suppressed app_keys.

        Passed to ``match_whitelist(exclude_app_keys=…)`` so the watcher
        skips suppressed specs entirely instead of finding the first match,
        seeing it's suppressed, and giving up — which let a declined
        background gmeet tab mask a foreground chatgpt-com match (user
        report 2026-04-25).
        """
        return frozenset(self.declined_release_at.keys())

    def mark_declined(self, app_key: str) -> None:
        """Mark the app as suppressed (decline OR session end). Cleared by
        ``tick_session`` once the app releases the mic for long enough."""
        # Start with None — no release streak yet (app may still be holding).
        self.declined_release_at[app_key] = None

    def tick_session(
        self,
        app_key: str,
        now_mono: float,
        *,
        holding_mic: bool,
        release_grace_secs: float,
    ) -> bool:
        """Per-poll update for session-based suppression.

        Returns True if the decline state was just cleared this call.
        """
        if app_key not in self.declined_release_at:
            return False
        if holding_mic:
            # App still holding — reset any release streak.
            self.declined_release_at[app_key] = None
            return False
        streak_start = self.declined_release_at[app_key]
        if streak_start is None:
            # First poll where the app isn't holding — start the streak.
            self.declined_release_at[app_key] = now_mono
            return False
        if now_mono - streak_start >= release_grace_secs:
            del self.declined_release_at[app_key]
            return True
        return False


class ArmController:
    """Single source of truth for armed state + arm-related background tasks."""

    def __init__(
        self,
        cfg: ArmConfig,
        detector: ConversationDetector,
        *,
        mic_capture,
        sys_capture,
        vad_mic,
        vad_sys,
        notifier: Notifier,
        # Optional path for seen-apps recording (populated from Config.data_dir
        # by the real Agent; None in tests → recording is a no-op).
        data_dir: Optional[Path] = None,
        # Optional platform query overrides (for tests).
        get_mic_holders: Optional[Callable[[], list[_d.MicHolder]]] = None,
        is_mic_active: Optional[Callable[[], bool]] = None,
        get_running_processes: Optional[Callable[[], frozenset[str]]] = None,
        get_foreground_info: Optional[Callable[[], ForegroundInfo]] = None,
        get_browser_window_titles: Optional[Callable[[], list[str]]] = None,
        get_browser_window_urls: Optional[Callable[[], list[str]]] = None,
        # Resolves PIDs for a whitelist-matched spec when the match
        # result doesn't already carry them (macOS path; Windows gets
        # PIDs directly from ``mic.holders`` so this returns ()).
        resolve_pids_for_spec: Optional[Callable[[DetectorSpec], tuple[int, ...]]] = None,
        # Web-onboarding gate. Called synchronously (no I/O — reads the
        # cached /api/me result) before flipping into ARMED on either the
        # hotkey or the whitelist consent path. ``None`` ⇒ no gating
        # (test default; production wires this from ``__main__``).
        account_gate_fn: Optional[AccountGateFn] = None,
        # Reads current ``cfg.capture.system_scope`` ("endpoint" or
        # "arm_app"). Callable, not snapshot — the Settings pane mutates
        # the live config object, so the next hotkey press needs the
        # fresh value. Default returns "endpoint" so tests + the
        # smoke-test harness don't have to pass it explicitly.
        system_scope_fn: Callable[[], str] = lambda: "endpoint",
        # Reads current ``cfg.hud.show_recording_indicator``. Same callable
        # pattern as ``system_scope_fn`` so the gate picks up Settings
        # mutations on the next arm without a service restart. Default
        # True so unit tests and any caller that constructs the controller
        # without wiring this up preserves the legacy "show pill on every
        # arm" behaviour.
        show_recording_indicator_fn: Callable[[], bool] = lambda: True,
    ) -> None:
        self.cfg = cfg
        self.detector = detector
        self.mic = mic_capture
        self.sys = sys_capture
        self.vad_mic = vad_mic
        self.vad_sys = vad_sys
        self.notifier = notifier
        # The HUD launcher (if any) — used to drive the persistent
        # capture pill (show / hide / collapse) on arm/disarm. Pulled
        # off the notifier so we don't need a separate constructor
        # argument. ``None`` for tests / NoopNotifier paths — every
        # pill-related call becomes a no-op.
        self._hud_launcher = getattr(notifier, "launcher", None)
        if self._hud_launcher is not None:
            self._hud_launcher.set_pill_stop_callback(self._on_pill_stop_clicked)
        self._data_dir = data_dir

        # Per-session dedup for seen-apps writes. Keyed by lower-cased
        # process name / bundle id — we write to disk the first time an
        # unmatched holder appears this session, and skip subsequent polls
        # even though the record call would dedup anyway (saves disk churn).
        self._recorded_seen: set[str] = set()

        self.state = ArmState.DISARMED
        self.armed_event: asyncio.Event = asyncio.Event()
        self._reason: Optional[ArmReason] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._hotkey: Optional[HotkeySource] = None
        self._whitelist_task: Optional[asyncio.Task] = None
        self._checkin_task: Optional[asyncio.Task] = None
        self._meeting_ended_task: Optional[asyncio.Task] = None
        self._cooldowns = _Cooldowns()

        # Last hotkey-press timestamp, for double-tap detection.
        self._last_hotkey_press: float = 0.0
        # Flag set when a confirmation toast is actively showing; a second
        # hotkey press during that window is treated as Yes.
        self._hotkey_confirmation_yes: Optional[asyncio.Future[None]] = None

        # Platform query injection — defaults picked per OS at wire time.
        self._q_mic_holders = get_mic_holders or _default_get_mic_holders()
        self._q_is_mic_active = is_mic_active or _default_is_mic_active()
        self._q_running_procs = get_running_processes or _default_get_running_processes()
        self._q_foreground = get_foreground_info or _default_get_foreground_info()
        self._q_browser_titles = (
            get_browser_window_titles or _default_get_browser_window_titles()
        )
        self._q_browser_urls = (
            get_browser_window_urls or _default_get_browser_window_urls()
        )
        self._q_resolve_pids_for_spec = (
            resolve_pids_for_spec or _default_resolve_pids_for_spec()
        )

        self._stop = asyncio.Event()

        # Drops a second tray click while a previous one is still mid-
        # transition. Without it, a rapid double-click could spawn two
        # overlapping tasks that both pass the "if self.state == X: return"
        # early-out (that check runs BEFORE mic.start awaits) and end up
        # double-starting streams + creating duplicate check-in tasks
        # (user report: "all the state gets fucked up"). A plain bool
        # works because we flip it synchronously before the first await,
        # so a concurrent entry always sees True.
        self._tray_transition_in_flight: bool = False

        # Optional callback fired after any arm/disarm transition completes.
        # Used by __main__'s _tray_bridge to push the new state to the tray
        # immediately instead of waiting for the next 0.5 s poll.
        self._state_change_callback: Optional[Callable[[], None]] = None

        # Web-onboarding gate (see _arm_internal + _run_whitelist_watcher).
        self.account_gate_fn: Optional[AccountGateFn] = account_gate_fn
        self._system_scope_fn = system_scope_fn
        self._show_recording_indicator_fn = show_recording_indicator_fn
        # Per-app_key dedup for the watcher's "skipped because gated" log,
        # so a long meeting in a blocked-account state doesn't spam at the
        # 2 s poll cadence.
        self._gated_log_keys: set[str] = set()

        # Wire the detector's pending-close hook.
        self.detector.on_pending_close = self._on_pending_close

    # ---- public lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Register hotkey and kick off the whitelist watcher. Call once,
        from ``Agent.run()``."""
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._hotkey = HotkeySource(self._loop, self._on_hotkey_pressed_sync)
        err = self._hotkey.register(self.cfg.hotkey)
        if err is not None:
            log.warning(
                "[arm] hotkey register failed (%s); tray menu remains as fallback",
                err,
            )
        self._whitelist_task = asyncio.create_task(
            self._run_whitelist_watcher(), name="arm-whitelist-watcher"
        )

    async def stop(self) -> None:
        """Tear down listeners + background tasks. Called on agent shutdown."""
        self._stop.set()
        if self.state == ArmState.ARMED:
            try:
                await self._disarm_internal(SessionCloseReason.SHUTDOWN, show_post_toast=False)
            except Exception:
                log.exception("[arm] disarm on shutdown failed")
        if self._hotkey is not None:
            self._hotkey.unregister()
            self._hotkey = None
        for task in (self._whitelist_task, self._checkin_task, self._meeting_ended_task):
            if task is not None and not task.done():
                task.cancel()
        self._whitelist_task = None
        self._checkin_task = None
        self._meeting_ended_task = None

    # ---- public API used by tray / settings / tests -----------------------

    @property
    def current_hotkey(self) -> str:
        return self._hotkey.binding if (self._hotkey and self._hotkey.binding) else self.cfg.hotkey

    def rebind_hotkey(self, new_binding: str) -> Optional[str]:
        """Swap the global hotkey. Returns None on success, or an error
        string suitable for the Settings window."""
        if self._hotkey is None:
            return "Hotkey listener not running"
        err = self._hotkey.rebind(new_binding)
        if err is None:
            self.cfg.hotkey = new_binding
        return err

    def reload_detectors(self) -> bool:
        """Re-read ``arm.detectors`` from ``user_settings.json`` and
        replace the in-memory list.

        Called over IPC from the Settings Meeting Apps pane after it
        mutates the on-disk JSON. The whitelist watcher reads
        ``self.cfg.detectors`` lazily on every poll, so the new list takes
        effect on the next tick (≤ ``poll_interval_secs``) — no need to
        bounce the watcher. A missing ``arm.detectors`` key is treated as
        "user cleared their override"; we restore the shipping defaults
        so behaviour matches a fresh install.

        Returns True on success, False when the on-disk JSON couldn't be
        read or validated. ``data_dir`` not configured (legacy tests)
        returns False without mutating state.
        """
        if self._data_dir is None:
            return False
        from .. import settings_store
        from ..config import DetectorSpec, default_detector_specs

        try:
            user = settings_store.load(self._data_dir)
        except Exception:
            log.warning("[arm] reload_detectors: settings_store.load failed", exc_info=True)
            return False

        arm_block = user.get("arm") if isinstance(user.get("arm"), dict) else {}
        raw = arm_block.get("detectors") if isinstance(arm_block, dict) else None

        if raw is None:
            self.cfg.detectors = default_detector_specs()
            log.info("[arm] reload_detectors: override cleared, restored defaults")
            return True
        if not isinstance(raw, list):
            return False
        try:
            new_list = [
                DetectorSpec.model_validate(d)
                for d in raw if isinstance(d, dict)
            ]
        except Exception:
            log.warning("[arm] reload_detectors: validation failed", exc_info=True)
            return False
        self.cfg.detectors = new_list
        log.info("[arm] reload_detectors: %d specs", len(new_list))
        return True

    def set_state_change_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """Register a callback invoked after every ARMED<->DISARMED flip.

        Called synchronously on the asyncio loop from ``_fire_state_change``.
        The callback MUST be non-blocking and thread-safe — the tray bridge
        uses it to push the new state to ``TrayState`` right away so menu
        labels don't lag the real arm state by up to one poll interval.
        """
        self._state_change_callback = cb

    def _fire_state_change(self) -> None:
        cb = self._state_change_callback
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.exception("[arm] state-change callback raised (non-fatal)")

    async def arm_from_tray(self) -> None:
        """Tray-menu click: flip state with no confirmation toast.

        The menu label IS the action — if the label says "Stop recording",
        clicking it stops. Surfacing a "Stop recording?" toast on top would
        be redundant and (combined with any label lag) is what the user saw
        as "it asks me in the toast to stop recording but then it still gets
        a hold of my mic". Hotkey disarms still confirm; the tray does not.

        Drops a second tray click while a previous transition is still in
        flight — queuing would produce the classic "I clicked stop and it
        armed again" flip-flop.
        """
        if self._tray_transition_in_flight:
            log.info("[arm] tray click ignored (transition in flight)")
            return
        self._tray_transition_in_flight = True
        try:
            if self.state == ArmState.ARMED:
                await self._disarm_internal(SessionCloseReason.HOTKEY_END)
            else:
                target_pids, mic_device = self._resolve_hotkey_arm()
                await self._arm_internal(
                    ArmReason(
                        source="hotkey",
                        target_pids=target_pids,
                        mic_device=mic_device,
                    )
                )
        finally:
            self._tray_transition_in_flight = False

    # ---- hotkey path -------------------------------------------------------

    def _on_hotkey_pressed_sync(self) -> None:
        """Called by the pynput listener thread via ``call_soon_threadsafe``."""
        if self._loop is None:
            return
        asyncio.ensure_future(self._on_hotkey_pressed(), loop=self._loop)

    async def _on_hotkey_pressed(self) -> None:
        now = time.monotonic()
        # Double-tap fast path: a second press within _DOUBLE_TAP_SECS while a
        # confirmation toast is showing acts as "Yes".
        if self._hotkey_confirmation_yes is not None and not self._hotkey_confirmation_yes.done():
            self._hotkey_confirmation_yes.set_result(None)
            self._last_hotkey_press = now
            return
        self._last_hotkey_press = now

        if self.state == ArmState.DISARMED:
            await self._confirm_and_arm()
        else:
            await self._disarm_with_confirm()

    async def _confirm_and_arm(self) -> None:
        """Show the start-confirmation toast. On Yes / double-tap, arm."""
        if await self._race_confirm_with_double_tap(
            "Start recording?",
            "Sayzo will capture this conversation so we can coach you on it.",
            "Yes, start", "Cancel",
        ):
            target_pids, mic_device = self._resolve_hotkey_arm()
            await self._arm_internal(
                ArmReason(
                    source="hotkey",
                    target_pids=target_pids,
                    mic_device=mic_device,
                )
            )

    async def _disarm_with_confirm(self) -> None:
        """Show the stop-confirmation toast. On Yes / double-tap, disarm.

        If the user has turned off ``ArmConfig.confirm_hotkey_stop`` in
        the Settings pane, skip the toast and disarm immediately —
        treating the hotkey press as the explicit decision the
        confirmation would have asked about.
        """
        if not self.cfg.confirm_hotkey_stop:
            await self._disarm_internal(SessionCloseReason.HOTKEY_END)
            return
        if await self._race_confirm_with_double_tap(
            "Stop recording?",
            "We'll save what we've captured so far.",
            "Yes, stop", "Keep going",  # default = Keep going
        ):
            await self._disarm_internal(SessionCloseReason.HOTKEY_END)

    def _on_pill_stop_clicked(self) -> None:
        """User clicked the HUD pill's stop button — disarm immediately.

        Called from :class:`HudLauncher`'s stdout reader, which runs on
        the agent's asyncio loop. The pill stop button is an explicit
        UI action, no confirmation is shown — going through the
        confirm-toast path on top of the click would feel double-tappy.
        """
        if self.state != ArmState.ARMED:
            return
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._disarm_internal(SessionCloseReason.HOTKEY_END), loop,
            )
        except Exception:
            log.warning("[arm] pill_stop schedule failed", exc_info=True)

    async def _race_confirm_with_double_tap(
        self, title: str, body: str, yes_label: str, no_label: str,
    ) -> bool:
        """Show a confirmation toast while also watching for a double-tap
        on the hotkey. Returns True on either "yes click" OR "double-tap";
        False on "no click" / timeout / error.

        We race the two paths with ``FIRST_COMPLETED`` so the transition
        fires the moment the user decides — not after the full
        ``hotkey_confirm_timeout_secs`` elapses. A prior version used
        ``asyncio.gather`` which waited for BOTH branches, making the arm /
        disarm land up to 10 s after the button click (user report: "the
        tray icon does not automatically say we are capturing").
        """
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        fut: asyncio.Future[None] = loop.create_future()
        self._hotkey_confirmation_yes = fut

        # Use the pill-pausing variant: when the hotkey is pressed
        # while DISARMED (_confirm_and_arm) there's no pill to pause
        # and the helper auto-no-ops via the ``_current_pill_params``
        # gate. When pressed while ARMED (_disarm_with_confirm) the
        # pill hides for the duration of the confirmation toast so
        # the user isn't simultaneously asked "stop?" and told "still
        # capturing".
        toast_task = asyncio.ensure_future(self._ask_consent_pausing_pill(
            title, body, yes_label, no_label,
            timeout_secs=self.cfg.hotkey_confirm_timeout_secs,
            default_on_timeout="no",
        ))
        try:
            done, pending = await asyncio.wait(
                {toast_task, fut},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._hotkey_confirmation_yes = None

        yes = False
        if fut in done:
            yes = True
        elif toast_task in done:
            try:
                yes = toast_task.result() == "yes"
            except Exception:
                yes = False
        # Release the still-pending branch. Cancelling ``toast_task`` here
        # only unblocks US; the OS toast continues until its own internal
        # timeout, and the notifier's executor thread sits on the sync
        # ``ask_consent`` until then. That's bounded (<= timeout + 5 s) so
        # we don't worry about leaking it.
        for task in pending:
            task.cancel()
        return yes

    # ---- arm / disarm internals -------------------------------------------

    async def _arm_internal(self, reason: ArmReason) -> None:
        """Transition DISARMED → ARMED.

        Flips state + fires the tray callback FIRST so the menu label and
        icon reflect "Recording" within milliseconds of the caller's
        decision, then opens both capture streams. Stream open can take
        50-500 ms on WASAPI / CoreAudio, and hiding the whole transition
        behind it meant the tray label lagged the user's Yes click by that
        amount (user report: "the tray icon does not automatically say we
        are capturing"). If stream open fails, we roll the state flip
        back below.
        """
        if self.state == ArmState.ARMED:
            return

        # Web-onboarding gate. Synchronous read of the cached /api/me
        # state — block here BEFORE any state mutation or stream open so
        # a gated arm leaves the controller cleanly in DISARMED. The
        # toast tells the user where to go next; for whitelist arms the
        # watcher already filtered upstream so this is the hotkey path's
        # last-line backstop.
        if self.account_gate_fn is not None:
            try:
                decision = self.account_gate_fn()
            except Exception:
                log.warning("[arm] account gate raised; allowing", exc_info=True)
                decision = AccountGateDecision(allowed=True)
            if not decision.allowed:
                log.info(
                    "[arm] gated by account-state %s — refusing to arm",
                    decision.reason,
                )
                if decision.toast_title and decision.toast_body:
                    self.notifier.notify(
                        decision.toast_title, decision.toast_body
                    )
                return

        # Fresh-start invariant: VAD counter + detector epoch rewind so the
        # next frame behaves as the first frame of a cold-started source.
        # Done before flipping armed_event so _consume doesn't process any
        # in-flight frame against stale VAD state. Then open the session
        # immediately at arm time — frames flow directly into the session
        # buffer (no pre-buffer indirection) so a stale frame from a
        # previous arm cycle can't accumulate zeros into next session's
        # backfill.
        arm_now = time.monotonic()
        try:
            self.vad_mic.reset()
            self.vad_sys.reset()
            self.detector.reset_per_source_streams()
            self.detector.open_session_on_arm(
                arm_now,
                arm_app_key=reason.app_key,
                arm_app_display=reason.display_name,
            )
        except Exception:
            log.exception("[arm] reset / open_session_on_arm failed (non-fatal)")

        self.state = ArmState.ARMED
        self._reason = reason
        self.armed_event.set()
        scope_desc = (
            f"app(pids={','.join(str(p) for p in reason.target_pids)})"
            if reason.target_pids else "endpoint"
        )
        log.info(
            "[arm] ARMED (reason=%s app=%s scope=%s mic=%s)",
            reason.source, reason.app_key or "-", scope_desc,
            reason.mic_device or "default",
        )
        self._fire_state_change()

        try:
            await self._start_mic_capture(reason.mic_device)
            await self._start_sys_capture(reason.target_pids)
        except Exception as exc:
            log.warning("[arm] capture start failed: %s", exc, exc_info=True)
            # Best-effort close whatever did open so we don't leak streams.
            # Log at debug — the parent log.warning above already named the
            # primary failure; this is "did the cleanup also fail?" signal.
            try:
                await self.mic.stop()
            except Exception:
                log.debug("[arm] mic.stop during start-rollback failed", exc_info=True)
            try:
                await self.sys.stop()
            except Exception:
                log.debug("[arm] sys.stop during start-rollback failed", exc_info=True)
            # Roll back the optimistic state flip. Tray briefly flashed
            # "Recording" — acceptable for this rare case; the error toast
            # plus the revert make the failure unambiguous.
            self.state = ArmState.DISARMED
            self._reason = None
            self.armed_event.clear()
            self._fire_state_change()
            self.notifier.notify(
                "Couldn't start capturing",
                "Sayzo couldn't access your microphone or speakers. "
                f"Try closing other recording apps, then press {self.current_hotkey} again.",
            )
            return

        # Show the persistent HUD pill — live timer + arm-reason label
        # + stop button. Mirrors the agent's arm state so the user
        # always knows it's running. The launcher remembers the kwargs
        # internally (``_last_pill_params``) so
        # ``ask_consent_pausing_pill`` can restore the same pill
        # verbatim (with the original ``start_ts``) after an "are you
        # still here?" consent if the user opts to keep going.
        #
        # Gated by ``cfg.hud.show_recording_indicator`` (read via the
        # ``show_recording_indicator_fn`` callable so a live Settings
        # toggle takes effect on the next arm) — users who picked "Stay
        # out of the way" during onboarding get no pill on arm. The tray
        # menu label still flips to "Stop recording" so they have a
        # non-floating confirmation that Sayzo is armed; consent cards /
        # toasts are unaffected (they fire on demand, not as a persistent
        # presence). ``hide_pill`` on disarm stays unconditional —
        # ``launcher.hide_pill`` is a no-op when no pill is showing.
        if (
            self._hud_launcher is not None
            and self._show_recording_indicator_fn()
        ):
            self._hud_launcher.show_pill(
                reason=reason.source,
                reason_label=(reason.display_name or reason.source.capitalize()),
                start_ts=time.time(),
                hotkey=self.current_hotkey,
            )

        # Post-arm guidance toast AFTER streams confirmed open, so we
        # don't tell the user we're capturing while the mic driver is
        # still coming up (and they might start speaking into a silent
        # window). Skipped when the Notifications pane's "Sayzo is
        # capturing" sub-toggle is off.
        if self.cfg.notify_post_arm:
            self.notifier.notify(
                "Sayzo is capturing",
                f"Press {self.current_hotkey} anytime to stop.",
            )

        # Record session-start time for the check-in marks. Use monotonic
        # now rather than the detector's _session_start_mono because a
        # session won't open until the first VAD segment fires.
        session_start_mono = time.monotonic()
        self._checkin_task = asyncio.create_task(
            self._run_checkins(session_start_mono), name="arm-checkin"
        )
        if reason.source == "whitelist" and reason.app_key:
            self._meeting_ended_task = asyncio.create_task(
                self._run_meeting_ended_watcher(reason), name="arm-meeting-ended"
            )

    def _flush_vads_into_detector(self, now: float) -> None:
        """Yield any in-progress VAD segments into the detector before the
        session is committed.

        The armed-only model closes sessions abruptly (hotkey_end, check-in
        wrap-up, meeting-ended, joint-silence-confirmed). VAD's normal
        hangover-close path requires ``hangover_ms`` (300 ms) of unvoiced
        chunks to actually emit the closing segment; an abrupt close never
        delivers them, and the still-open segment would be discarded when
        the next arm calls ``vad.reset()``. Flushing here recovers it.

        Matches the replay-teardown shape in ``__main__.py``'s replay
        command verbatim. Non-fatal on raise — the session still commits
        with whatever segments did close normally.
        """
        try:
            for seg in self.vad_mic.flush():
                self.detector.on_segment(seg, now)
        except Exception:
            log.exception("[arm] vad_mic flush before close failed (non-fatal)")
        try:
            for seg in self.vad_sys.flush():
                self.detector.on_segment(seg, now)
        except Exception:
            log.exception("[arm] vad_sys flush before close failed (non-fatal)")

    async def _disarm_internal(
        self,
        close_reason: SessionCloseReason,
        *,
        show_post_toast: bool = False,
    ) -> None:
        """Transition ARMED → DISARMED.

        Flips state + fires the tray callback FIRST so the menu label
        updates within milliseconds of the user's Yes click. Stopping the
        system-audio thread joins with up to ~500 ms of in-flight batch
        read; blocking the tray update behind that felt sluggish on
        Windows. Clearing ``armed_event`` before the actual stream stops
        means ``_consume`` stops pulling immediately — any frames still
        arriving until the streams close sit in the queue unconsumed.
        """
        if self.state == ArmState.DISARMED:
            return
        # Force-close any session in flight (OPEN or PENDING_CLOSE). This
        # enqueues buffers for _process_session to pick up; we don't wait.
        try:
            now = time.monotonic()
            if self.detector.state in (SessionState.OPEN, SessionState.PENDING_CLOSE):
                self._flush_vads_into_detector(now)
                self.detector.commit_close(now, close_reason)
        except Exception:
            log.exception("[arm] detector commit_close failed")

        # Unified "processing" toast on every clean disarm — hotkey,
        # pill click, check-in wrap-up, whitelist-ended, silence-close-
        # confirmed. Skipped for SHUTDOWN (agent is going down) and any
        # error/abort reason added later. The discard / saved toasts
        # downstream (see app.py + upload_retry.py) close the loop on
        # what actually happened to the capture.
        _processing_reasons = (
            SessionCloseReason.HOTKEY_END,
            SessionCloseReason.CHECKIN_WRAP_UP,
            SessionCloseReason.WHITELIST_ENDED,
            SessionCloseReason.JOINT_SILENCE,
        )
        if close_reason in _processing_reasons:
            try:
                self.notifier.notify(
                    "Got it",
                    "Processing your capture. You'll see a notification when it's ready.",
                )
            except Exception:
                log.debug("[arm] processing toast failed", exc_info=True)

        for task in (self._checkin_task, self._meeting_ended_task):
            if task is not None and not task.done():
                task.cancel()
        self._checkin_task = None
        self._meeting_ended_task = None

        # Suppress re-prompts for this app until it releases the mic for
        # ``decline_release_grace_secs`` continuous seconds. Reuses the
        # decline path's release-tracking so closing + reopening a session
        # (e.g. ChatGPT voice mode) immediately re-prompts on the new
        # mic acquisition, while staying quiet if the app keeps holding
        # the mic (still in the same Zoom call).
        prev = self._reason
        if prev is not None and prev.app_key:
            self._cooldowns.mark_declined(prev.app_key)

        self.state = ArmState.DISARMED
        self._reason = None
        self.armed_event.clear()
        log.info("[arm] DISARMED (reason=%s)", close_reason.value)
        self._fire_state_change()

        # Hide the HUD pill the moment the state flip lands — same
        # ordering rationale as the tray-callback fire (UI should
        # reflect the user's decision before stream tear-down latency).
        # The launcher's ``hide_pill`` clears its
        # ``_last_pill_params`` snapshot, which is what makes any
        # in-flight ``ask_consent_pausing_pill`` skip its restore.
        if self._hud_launcher is not None:
            self._hud_launcher.hide_pill()

        try:
            await self.mic.stop()
        except Exception:
            log.exception("[arm] mic.stop failed")
        try:
            await self.sys.stop()
        except Exception:
            log.exception("[arm] sys.stop failed")

    # ---- detector hook ----------------------------------------------------

    def _on_pending_close(self) -> None:
        """Detector just flipped OPEN → PENDING_CLOSE on joint silence.
        Schedule the end-confirmation toast on our loop."""
        if self._loop is None:
            return
        asyncio.ensure_future(self._handle_pending_close(), loop=self._loop)

    async def _handle_pending_close(self) -> None:
        result = await self._ask_consent_pausing_pill(
            "Was that the end of your meeting?",
            "It's been quiet for a bit. Wrap up and save, or keep going?",
            "Yes, done", "Not yet",
            timeout_secs=self.cfg.end_toast_timeout_secs,
            default_on_timeout="yes",  # default Wrap up on non-response
        )
        now = time.monotonic()
        # If the detector auto-reverted (user started speaking during the
        # toast), we're already back to OPEN — drop the toast result.
        if self.detector.state == SessionState.OPEN:
            log.info("[arm] end-confirmation discarded (detector auto-reverted)")
            return
        if self.detector.state != SessionState.PENDING_CLOSE:
            # Race: session already committed by another path.
            return
        if result in ("yes", "timeout"):
            self._flush_vads_into_detector(now)
            self.detector.commit_close(now, SessionCloseReason.JOINT_SILENCE)
            await self._disarm_internal(SessionCloseReason.JOINT_SILENCE)
        else:  # "no" → Not yet
            self.detector.revert_close(now)

    # ---- whitelist watcher (runs while DISARMED) -------------------------

    async def _run_whitelist_watcher(self) -> None:
        log.info(
            "[arm] whitelist watcher started (poll=%.1fs, %d detectors)",
            self.cfg.poll_interval_secs, len(self.cfg.detectors),
        )
        # Diagnostic dedup key for the no-match log below — see the
        # call site for what it covers and when it gets reset.
        last_no_match_signature: Optional[tuple] = None
        # Time-throttle layered on top of the signature dedup. Background
        # dictation apps (Apple CoreSpeech, Wispr Flow, etc.) flicker the
        # holder set every few seconds and defeat the signature dedup,
        # producing hundreds of log lines per hour on otherwise-idle
        # machines. Even after a new signature appears, we suppress
        # repeat logs within ``_NO_MATCH_LOG_THROTTLE_SECS`` of the last
        # one — except for the very first emission per watcher start,
        # so a tailing log can see we're alive.
        last_no_match_log_at: float = 0.0
        try:
            last_match_key: Optional[str] = None
            while not self._stop.is_set():
                try:
                    await asyncio.sleep(self.cfg.poll_interval_secs)
                except asyncio.CancelledError:
                    return
                if self.state != ArmState.DISARMED:
                    continue
                try:
                    mic = self._snapshot_mic_state()
                    fg = self._snapshot_foreground()
                except Exception:
                    log.debug("[arm] whitelist snapshot failed", exc_info=True)
                    continue
                now_mono = time.monotonic()
                # Tick the session-based decline tracker for every declined
                # app. Clearing happens when the app has been off the mic
                # for decline_release_grace_secs continuous seconds —
                # "leaving the meeting counts as a new session".
                for app_key in list(self._cooldowns.declined_release_at.keys()):
                    holding = _d.arm_app_still_holding_mic(
                        app_key, self.cfg.detectors, mic, fg,
                    )
                    cleared = self._cooldowns.tick_session(
                        app_key, now_mono,
                        holding_mic=holding,
                        release_grace_secs=self.cfg.decline_release_grace_secs,
                    )
                    if cleared:
                        log.info(
                            "[arm] decline cleared for %s (mic released — "
                            "fresh prompt on next match)",
                            app_key,
                        )
                # Skip suppressed app_keys directly inside match_whitelist
                # so a declined gmeet (still "holding" via background tab)
                # can't shadow a chatgpt-com match in the foreground —
                # without this filter the watcher would find gmeet first,
                # see it's suppressed, and silently bail for the whole poll.
                suppressed = self._cooldowns.suppressed_keys()
                match = _d.match_whitelist(
                    self.cfg.detectors, fg, mic,
                    exclude_app_keys=suppressed,
                )
                if match is None:
                    last_match_key = None
                    # Diagnostic for "watcher silently doesn't fire" reports:
                    # whenever the mic is active but nothing matched, log
                    # what we saw. Dedup signature deliberately excludes
                    # window titles — editors that animate spinners
                    # ("⠂ ⠐ ⠐ ✳ …") flip a title every poll, defeating
                    # dedup and producing one log line per 2 s tick (user
                    # report 2026-05-07). Holder set + foreground app +
                    # tab URL are the signals that actually drive matching;
                    # titles are a fallback we still log in the message
                    # body for context, just not in the dedup key.
                    if mic.active:
                        holder_summary = tuple(sorted(
                            f"{h.bundle_id or h.process_name}#{h.pid}"
                            for h in mic.holders
                        ))
                        sorted_browser_urls = tuple(sorted(set(fg.browser_window_urls)))
                        signature = (
                            holder_summary,
                            fg.bundle_id,
                            fg.is_browser,
                            fg.browser_tab_url,
                            sorted_browser_urls,
                        )
                        if signature != last_no_match_signature:
                            last_no_match_signature = signature
                            throttle_ok = (
                                last_no_match_log_at == 0.0
                                or (now_mono - last_no_match_log_at)
                                >= _NO_MATCH_LOG_THROTTLE_SECS
                            )
                            if throttle_ok:
                                last_no_match_log_at = now_mono
                                sig_titles = tuple(
                                    sorted({
                                        fg.browser_tab_title or "",
                                        fg.window_title or "",
                                        *fg.browser_window_titles,
                                    } - {""})
                                )
                                log.info(
                                    "[arm] no whitelist match: holders=%s "
                                    "fg.bundle=%s fg.is_browser=%s "
                                    "tab_title=%r win_titles=%r url=%r — "
                                    "active_specs=%d (suppressed=%d)",
                                    list(holder_summary) or "[]",
                                    fg.bundle_id,
                                    fg.is_browser,
                                    fg.browser_tab_title,
                                    list(sig_titles),
                                    fg.browser_tab_url,
                                    len(self.cfg.detectors),
                                    len(suppressed),
                                )
                    elif last_no_match_signature is not None:
                        last_no_match_signature = None
                    self._record_unmatched_holders(mic, fg)
                    continue
                last_no_match_signature = None
                # One-shot INFO log per match transition so field debugging
                # doesn't require raising the global log level.
                if match.app_key != last_match_key:
                    log.info(
                        "[arm] whitelist matched %s (%s) via %s",
                        match.display_name, match.app_key, match.source,
                    )
                    last_match_key = match.app_key

                # Web-onboarding gate. Skip the consent toast silently —
                # we don't want to ask the user to start recording when
                # we'd refuse to arm even on Yes. The hotkey path's gate
                # is what tells the user about onboarding (it's their
                # explicit ask). Dedup'd per app_key transition so a long
                # blocked session doesn't spam the log at the 2 s poll.
                if self.account_gate_fn is not None:
                    try:
                        decision = self.account_gate_fn()
                    except Exception:
                        log.warning(
                            "[arm] account gate raised in watcher; allowing",
                            exc_info=True,
                        )
                        decision = AccountGateDecision(allowed=True)
                    if not decision.allowed:
                        if match.app_key not in self._gated_log_keys:
                            log.info(
                                "[arm] whitelist watcher: skipped consent for "
                                "%s — account gated (%s)",
                                match.app_key, decision.reason,
                            )
                            self._gated_log_keys.add(match.app_key)
                        continue
                    elif self._gated_log_keys:
                        # Account became OK — clear the dedup so a future
                        # gating event logs again.
                        self._gated_log_keys.clear()

                log.info("[arm] firing consent toast for %s", match.app_key)
                # Show consent toast.
                result = await self._ask_consent(
                    "Sayzo is ready to coach you",
                    f"Looks like you're in {match.display_name}. "
                    "Want us to capture this so we can highlight your coachable moments?",
                    "Start coaching", "Not now",
                    timeout_secs=self.cfg.consent_toast_timeout_secs,
                    default_on_timeout="no",
                )
                log.info("[arm] consent toast for %s → %s", match.app_key, result)
                if self.state != ArmState.DISARMED:
                    # User armed via hotkey while the toast was up. Drop.
                    continue
                if result == "yes":
                    target_pids, mic_device = self._resolve_arm_scope_for_match(match)
                    await self._arm_internal(
                        ArmReason(
                            source="whitelist",
                            app_key=match.app_key,
                            display_name=match.display_name,
                            target_pids=target_pids,
                            mic_device=mic_device,
                        )
                    )
                else:
                    # Decline / timeout → session-based suppression. Clears
                    # once the app releases the mic; the hotkey still works
                    # as a manual opt-in in the meantime.
                    self._cooldowns.mark_declined(match.app_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[arm] whitelist watcher crashed")

    # ---- long-meeting check-in (runs while ARMED) ------------------------

    async def _run_checkins(self, session_start_mono: float) -> None:
        if not self.cfg.checkin_enabled:
            return
        try:
            for mark in sorted(self.cfg.long_meeting_checkin_marks_secs):
                # Sleep until this mark elapses.
                while self.state == ArmState.ARMED and not self._stop.is_set():
                    elapsed = time.monotonic() - session_start_mono
                    remaining = mark - elapsed
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.sleep(min(remaining, 60.0))
                    except asyncio.CancelledError:
                        return
                if self.state != ArmState.ARMED:
                    return
                # Fire the check-in toast.
                elapsed = time.monotonic() - session_start_mono
                result = await self._ask_consent_pausing_pill(
                    "Still in the meeting?",
                    f"Sayzo has been capturing for {_human_duration(elapsed)}. "
                    "Keep going, or wrap up?",
                    "Yes, keep going", "Wrap up",
                    timeout_secs=self.cfg.checkin_toast_timeout_secs,
                    default_on_timeout="yes",  # default Keep going
                )
                # "no" → Wrap up; "yes" / "timeout" → continue to next mark.
                if result == "no":
                    await self._disarm_internal(SessionCloseReason.CHECKIN_WRAP_UP)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[arm] check-in task crashed")

    # ---- meeting-ended watcher (runs while whitelist-ARMED) --------------

    async def _run_meeting_ended_watcher(self, reason: ArmReason) -> None:
        assert reason.app_key is not None
        if not self.cfg.meeting_ended_watcher_enabled:
            return
        try:
            grace = 0.0
            # ``keep_going_clicked`` switches the watcher into silent
            # force-close mode: after the user explicitly declined the
            # first toast we don't ask again, but we still track the
            # arm-app's mic-holder absence and close the session if it
            # crosses ``force_close_after_keep_going_secs``. The previous
            # design (10-min ``snooze_until``) just stopped checking,
            # which meant a meeting that genuinely ended a few minutes
            # after "Keep going" still captured ~10 minutes of nothing
            # before the next toast fired.
            keep_going_clicked = False
            absence_after_keep_going = 0.0
            while self.state == ArmState.ARMED and not self._stop.is_set():
                try:
                    await asyncio.sleep(self.cfg.poll_interval_secs)
                except asyncio.CancelledError:
                    return
                try:
                    mic = self._snapshot_mic_state()
                    fg = self._snapshot_foreground()
                except Exception:
                    log.debug("[arm] meeting-ended snapshot failed", exc_info=True)
                    continue
                still = _d.arm_app_still_holding_mic(
                    reason.app_key, self.cfg.detectors, mic, fg,
                )
                if still:
                    grace = 0.0
                    absence_after_keep_going = 0.0
                    continue

                if keep_going_clicked:
                    # Post-"Keep going" path. User already saw and
                    # declined the interactive toast; we don't re-toast
                    # interactively — but we DO accumulate consecutive
                    # absence (resets to 0 the moment the arm-app comes
                    # back into mic-holders) and close the session when
                    # it crosses the threshold. A friendly informational
                    # toast (no buttons) tells them what happened so the
                    # close isn't silent and confusing.
                    absence_after_keep_going += self.cfg.poll_interval_secs
                    if (
                        absence_after_keep_going
                        >= self.cfg.force_close_after_keep_going_secs
                    ):
                        log.info(
                            "[arm] mic released for %.0fs after 'Keep going' "
                            "— wrapping up the session (app=%s)",
                            absence_after_keep_going, reason.app_key,
                        )
                        if self.cfg.notify_session_wrapped:
                            name = reason.display_name or "your meeting app"
                            try:
                                self.notifier.notify(
                                    "Wrapped up your session",
                                    f"Looks like {name} stayed quiet for a while — "
                                    "Sayzo saved what you had.",
                                )
                            except Exception:
                                log.debug(
                                    "[arm] info toast on force-close failed",
                                    exc_info=True,
                                )
                        await self._disarm_internal(
                            SessionCloseReason.WHITELIST_ENDED
                        )
                        return
                    continue

                grace += self.cfg.poll_interval_secs
                if grace < self.cfg.whitelist_arm_release_grace_secs:
                    continue
                # Fire meeting-ended toast.
                name = reason.display_name or "your meeting app"
                result = await self._ask_consent_pausing_pill(
                    "Looks like your meeting ended",
                    f"Sayzo noticed {name} stopped using the microphone. "
                    "Wrap up and save, or keep going?",
                    "Wrap up", "Keep going",
                    timeout_secs=self.cfg.meeting_ended_toast_timeout_secs,
                    default_on_timeout="yes",  # default Wrap up
                )
                if result in ("yes", "timeout"):
                    await self._disarm_internal(SessionCloseReason.WHITELIST_ENDED)
                    return
                # "Keep going" → switch to silent force-close mode and
                # reset counters so the threshold is measured from
                # *now*, not from the (possibly long) absence that
                # triggered the toast in the first place.
                keep_going_clicked = True
                grace = 0.0
                absence_after_keep_going = 0.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[arm] meeting-ended watcher crashed")

    # ---- helpers ----------------------------------------------------------

    def _resolve_arm_scope_for_match(
        self, match: _d.MatchResult,
    ) -> tuple[tuple[int, ...], Optional[str]]:
        """Resolve the (target_pids, mic_device) scope to use when arming
        for this match.

        - ``target_pids`` — prefer ``match.target_pids`` (filled on
          Windows from mic.holders, and on the macOS desktop-app path).
          Fall back to the injected resolver (macOS → psutil +
          NSWorkspace) when the match was a mac browser path that
          couldn't attribute PIDs inline. Empty tuple ⇒ endpoint-wide
          system-audio capture.
        - ``mic_device`` — peek into the current mic-holder snapshot to
          find the capture device the matched app is using, so
          ``MicCapture`` opens that device instead of the OS default.
          Picks a holder whose PID is in ``target_pids`` (= matched the
          spec) and whose ``device_name`` is set. If the match scoped
          to multiple PIDs on different devices (e.g. Zoom + Slack
          huddle both whitelisted under the same app_key — vanishingly
          rare), the first device wins with an INFO log so we can spot
          the case in the wild.
        """
        if match.target_pids:
            target_pids = match.target_pids
        else:
            spec = next(
                (s for s in self.cfg.detectors if s.app_key == match.app_key),
                None,
            )
            if spec is None:
                return ((), None)
            try:
                pids = self._q_resolve_pids_for_spec(spec) or ()
            except Exception:
                log.debug(
                    "[arm] resolve_pids_for_spec(%s) failed — falling back to endpoint scope",
                    match.app_key, exc_info=True,
                )
                return ((), None)
            target_pids = tuple(pids)

        mic_device = self._pick_mic_device_for_pids(target_pids, label=match.app_key or "?")
        return target_pids, mic_device

    def _pick_mic_device_for_pids(
        self, target_pids: tuple[int, ...], *, label: str,
    ) -> Optional[str]:
        """Find the capture device a member of ``target_pids`` is holding.

        Returns the first ``device_name`` we see across the current
        mic-holders whose PID is in ``target_pids``. ``None`` when the
        target list is empty, when no holder matches (the holder list
        moved on between match and arm — rare race; the caller falls
        back to OS default), or when none of the matching holders had
        a device name populated (Windows endpoint without friendly
        name, macOS pre-Swift-helper-upgrade).

        Logs a one-shot INFO line if multiple distinct devices show up
        across the matching holders — that's the unusual case
        documented in the plan (e.g. two whitelisted apps on different
        endpoints under one app_key).
        """
        if not target_pids:
            return None
        try:
            mic = self._snapshot_mic_state()
        except Exception:
            log.debug(
                "[arm] mic snapshot for device-routing failed (label=%s)",
                label, exc_info=True,
            )
            return None
        target_set = set(target_pids)
        matching_devices: list[str] = []
        for holder in mic.holders:
            if holder.pid not in target_set:
                continue
            if not holder.device_name:
                continue
            if holder.device_name not in matching_devices:
                matching_devices.append(holder.device_name)
        if not matching_devices:
            return None
        if len(matching_devices) > 1:
            log.info(
                "[arm] %s holders on %d different devices %r — using %r",
                label, len(matching_devices), matching_devices,
                matching_devices[0],
            )
        return matching_devices[0]

    async def _start_sys_capture(self, target_pids: tuple[int, ...]) -> None:
        """Call ``SystemCapture.start`` with a ``target_pids`` kwarg when
        supported, falling back to the no-arg signature.

        Allows test fakes and pre-phase-B/C captures that don't yet accept
        the kwarg to keep working unmodified.
        """
        try:
            await self.sys.start(target_pids=target_pids)
        except TypeError:
            await self.sys.start()

    async def _start_mic_capture(self, device: Optional[str]) -> None:
        """Call ``MicCapture.start`` with a ``device`` kwarg when supported,
        falling back to the no-arg signature.

        Mirrors ``_start_sys_capture``'s TypeError fallback so legacy test
        fakes that don't accept ``device`` keep passing. Production
        ``MicCapture.start`` always accepts the kwarg (v2.7.12+).
        """
        try:
            await self.mic.start(device=device)
        except TypeError:
            await self.mic.start()

    def _resolve_hotkey_arm(self) -> tuple[tuple[int, ...], Optional[str]]:
        """Decide ``(target_pids, mic_device)`` for a hotkey-triggered arm.

        Default (``system_scope=endpoint``, v2.9+ shipping default):
        captures the whole endpoint regardless of which app holds the
        mic. ``target_pids`` is ``()`` — no per-app scoping logic runs.
        The MIC is still routed opportunistically to whatever device a
        holder is using; recording where the mic is live beats
        recording the built-in mic pointed at empty air. Unknown
        holders are recorded to ``seen_apps`` for one-click promotion
        in Settings.

        Beta (``system_scope=arm_app``, Settings → Recording →
        "Per-app audio capture (beta)"): narrows scope per-app when a
        whitelisted holder is present. Unknown holder → endpoint
        fallback (the v2.7.9 fix that prevents silent capture when
        Steam Voice / ChatGPT-voice / Voice Recorder is the only
        mic-holder).

        Mic-device routing rules are the same in both modes — always
        opportunistic.
        """
        try:
            mic = self._snapshot_mic_state()
            fg = self._snapshot_foreground()
        except Exception:
            log.debug("[arm] hotkey resolve snapshot failed", exc_info=True)
            return ((), None)

        if sys.platform == "win32":
            return self._resolve_hotkey_arm_win(mic)
        if sys.platform == "darwin":
            return self._resolve_hotkey_arm_mac(mic, fg)
        return ((), None)

    def _resolve_hotkey_arm_win(
        self, mic: MicState,
    ) -> tuple[tuple[int, ...], Optional[str]]:
        if not mic.holders:
            return ((), None)
        holder_names = sorted({h.process_name for h in mic.holders if h.process_name})
        first_device = next(
            (h.device_name for h in mic.holders if h.device_name), None,
        )

        if self._system_scope_fn() != "arm_app":
            log.info(
                "[arm] hotkey scope: endpoint (mic-holders: %s) mic=%r",
                ", ".join(holder_names) or "?", first_device or "default",
            )
            try:
                self._record_unmatched_holders(mic, ForegroundInfo())
            except Exception:
                log.debug("[arm] hotkey seen_apps.record failed", exc_info=True)
            return (), first_device

        # Beta path: try to narrow to a whitelisted holder. Reuses the
        # same matcher the whitelist auto-arm path uses (handles bundle
        # id + helper-bundle prefix matching, not just process_name).
        whitelisted_pids: set[int] = set()
        for spec in self.cfg.detectors:
            if spec.disabled or spec.is_browser:
                continue
            whitelisted_pids.update(_d._pids_for_desktop_holders(spec, mic))

        if whitelisted_pids:
            whitelisted_device = next(
                (h.device_name for h in mic.holders
                 if h.pid in whitelisted_pids and h.device_name),
                None,
            )
            log.info(
                "[arm] hotkey scope: per-app pids=%s (beta) mic=%r",
                ",".join(str(p) for p in sorted(whitelisted_pids)),
                whitelisted_device or "default",
            )
            return tuple(sorted(whitelisted_pids)), whitelisted_device

        # Beta on but no whitelisted holder — fall back to endpoint to
        # avoid silent capture from wrong-app mic-holders (Steam Voice,
        # ChatGPT voice, Voice Recorder, …). v2.7.9 fix.
        log.info(
            "[arm] hotkey scope: endpoint (beta on, no whitelisted holder "
            "among %s) mic=%r",
            ", ".join(holder_names) or "?", first_device or "default",
        )
        try:
            self._record_unmatched_holders(mic, ForegroundInfo())
        except Exception:
            log.debug("[arm] hotkey seen_apps.record failed", exc_info=True)
        return (), first_device

    def _resolve_hotkey_arm_mac(
        self, mic: MicState, fg: ForegroundInfo,
    ) -> tuple[tuple[int, ...], Optional[str]]:
        if not mic.active:
            return ((), None)
        first_device = next(
            (h.device_name for h in mic.holders if h.device_name), None,
        )

        if self._system_scope_fn() != "arm_app":
            # Default endpoint path. Record unmatched foreground for
            # the "Suggested to add" UI even when we're not scoping.
            log.info(
                "[arm] hotkey scope: endpoint mic=%r", first_device or "default",
            )
            try:
                self._record_unmatched_holders(mic, fg)
            except Exception:
                log.debug("[arm] hotkey seen_apps.record failed", exc_info=True)
            return ((), first_device)

        # Beta path: foreground-driven per-app scoping.
        fg_proc = (fg.process_name or "").lower()
        fg_bundle = (fg.bundle_id or "").lower()
        if not fg_proc and not fg_bundle:
            # No foreground attribution to match against — fall back to
            # endpoint with opportunistic mic routing.
            log.info(
                "[arm] hotkey scope: endpoint (beta on, no foreground "
                "attribution) mic=%r", first_device or "default",
            )
            return ((), first_device)

        for spec in self.cfg.detectors:
            if spec.disabled:
                continue
            targets = {b.lower() for b in spec.bundle_ids} | {
                p.lower() for p in spec.process_names
            }
            if (fg_proc and fg_proc in targets) or (fg_bundle and fg_bundle in targets):
                try:
                    pids = tuple(self._q_resolve_pids_for_spec(spec) or ())
                except Exception:
                    log.debug(
                        "[arm] resolve_pids_for_spec(%s) raised — "
                        "continuing with foreground-only fallback",
                        spec.app_key, exc_info=True,
                    )
                    pids = ()
                if pids:
                    device = self._pick_mic_device_for_pids(pids, label=spec.app_key)
                    log.info(
                        "[arm] hotkey scope: per-app pids=%d (beta, %s) mic=%r",
                        len(pids), spec.app_key, device or "default",
                    )
                    return pids, device
                # Resolver returned empty — fall through to endpoint.
                break

        # Beta on but foreground isn't whitelisted (or resolver returned
        # empty). Fall back to endpoint to avoid silent capture.
        log.info(
            "[arm] hotkey scope: endpoint (beta on, foreground %s not "
            "whitelisted) mic=%r",
            fg_bundle or fg_proc or "?", first_device or "default",
        )
        try:
            self._record_unmatched_holders(mic, fg)
        except Exception:
            log.debug("[arm] hotkey seen_apps.record failed", exc_info=True)
        return (), first_device

    def _snapshot_mic_state(self) -> MicState:
        holders = self._q_mic_holders() or []
        # Common case: holders is non-empty → mic is active by definition,
        # skip the second platform call. The fallback only fires when we
        # have no attributed holders but the mic might still be active
        # (rare edge case on macOS where audio_detect saw an unattributable
        # capturing process; consulted by the seen-apps recorder + the
        # hotkey resolver's foreground path on macOS).
        active = bool(holders) or bool(self._q_is_mic_active())
        running = self._q_running_procs() or frozenset()
        return MicState(holders=list(holders), active=active, running_processes=running)

    def snapshot_mic_state(self) -> MicState:
        """Public mic-holder snapshot for the Settings Meeting Apps pane.

        The Settings GUI opens on a worker thread; calling this directly is
        safe — on Windows the query hops onto the dedicated COM executor,
        and macOS's audio-detect helper is invoked synchronously per call.
        """
        return self._snapshot_mic_state()

    def snapshot_foreground(self) -> ForegroundInfo:
        """Public foreground-info snapshot (matches :meth:`snapshot_mic_state`).

        Used by the Add-app dialog's Desktop tab to suppress browser
        processes from the live picker — browser-based web meetings are
        added via the Web tab instead.
        """
        return self._snapshot_foreground()

    def _record_unmatched_holders(self, mic: MicState, fg: ForegroundInfo) -> None:
        """Persist any mic-holders we saw this poll that aren't on the list.

        - No data_dir wired in (tests) → no-op.
        - Browsers are skipped: they hold the mic for web meetings, but the
          user-facing fix is to add the site in the Web tab, not add
          ``chrome.exe`` as a desktop app.
        - Dedups in-memory per session to avoid writing to disk every 2 s
          while a long-running unknown meeting app is holding the mic.
        """
        if self._data_dir is None:
            return

        # Windows: direct mic-holder names.
        for holder in mic.holders:
            key = (holder.process_name or "").lower()
            if not key:
                continue
            if key in _d.BROWSER_PROCESS_NAMES:
                continue
            if key in self._recorded_seen:
                continue
            display = _seen_apps._display_name_for_process(holder.process_name)
            try:
                _seen_apps.record(
                    self._data_dir,
                    key=key,
                    display_name=display,
                    whitelist=self.cfg.detectors,
                    process_name=holder.process_name,
                )
            except Exception:
                log.debug("[arm] seen_apps.record failed", exc_info=True)
            self._recorded_seen.add(key)

        # macOS: no per-process attribution. Fall back to the foreground
        # bundle id when the mic is active — our best guess for who's
        # capturing. Skip if the foreground is a browser.
        if sys.platform == "darwin" and mic.active and fg.bundle_id:
            if not fg.is_browser:
                key = fg.bundle_id.lower()
                if key and key not in self._recorded_seen:
                    display = _seen_apps._display_name_for_bundle(fg.bundle_id)
                    try:
                        _seen_apps.record(
                            self._data_dir,
                            key=key,
                            display_name=display,
                            whitelist=self.cfg.detectors,
                            bundle_id=fg.bundle_id,
                        )
                    except Exception:
                        log.debug("[arm] seen_apps.record failed", exc_info=True)
                    self._recorded_seen.add(key)

    def _snapshot_foreground(self) -> ForegroundInfo:
        """Foreground info enriched with all visible browser window titles
        + active-tab URLs.

        The platform ``get_foreground_info`` query only knows about the
        frontmost window; we layer ``browser_window_titles`` +
        ``browser_window_urls`` on top so the matcher can attribute a
        browser mic-hold to the right meeting tab even when the user
        Alt+Tabs to a non-browser.
        """
        fg = self._q_foreground()
        try:
            titles = self._q_browser_titles() or []
        except Exception:
            log.debug("[arm] browser-titles query failed", exc_info=True)
            titles = []
        try:
            urls = self._q_browser_urls() or []
        except Exception:
            log.debug("[arm] browser-urls query failed", exc_info=True)
            urls = []
        if not titles and not urls:
            return fg
        from dataclasses import replace
        return replace(
            fg,
            browser_window_titles=tuple(titles),
            browser_window_urls=tuple(urls),
        )

    async def _ask_consent(
        self, title: str, body: str, yes: str, no: str, timeout_secs: float,
        default_on_timeout: ConsentResult,
    ) -> ConsentResult:
        """Run ``Notifier.ask_consent`` without blocking the event loop.

        The notifier's ``ask_consent`` is sync (blocks on a concurrent Future).
        We run it in a default executor so our asyncio loop keeps ticking —
        important because other watchers (whitelist, check-in, meeting-ended)
        may need to continue polling while a toast is up.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.notifier.ask_consent(
                title, body, yes, no, timeout_secs, default_on_timeout=default_on_timeout,
            ),
        )

    async def _ask_consent_pausing_pill(
        self, title: str, body: str, yes: str, no: str, timeout_secs: float,
        default_on_timeout: ConsentResult,
    ) -> ConsentResult:
        """Ask for consent with the persistent pill hidden during the prompt.

        Used by the four "are you still in a meeting?" flows
        (joint-silence pending-close, long-meeting check-in,
        meeting-ended watcher, hotkey-pressed-while-armed end
        confirmation). Showing the live waveform pill while asking
        "are you done?" is visually noisy.

        Implementation lives on ``HudLauncher.ask_consent_pausing_pill``
        — the launcher tracks the active pill kwargs internally as
        ``_last_pill_params``, so a disarm-during-consent (via
        ``_disarm_internal`` → ``launcher.hide_pill`` → clears
        ``_last_pill_params``) automatically skips the restore.
        Falls back to a plain ``_ask_consent`` when no HUD is wired
        (NoopNotifier path in unit tests).
        """
        if self._hud_launcher is None:
            return await self._ask_consent(
                title, body, yes, no, timeout_secs,
                default_on_timeout=default_on_timeout,
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._hud_launcher.ask_consent_pausing_pill(
                title, body, yes, no, timeout_secs,
                default_on_timeout=default_on_timeout,
            ),
        )


# ---- platform query resolution ------------------------------------------


def _default_get_mic_holders():
    if sys.platform == "win32":
        from .platform_win import get_mic_holders as fn
        return fn
    if sys.platform == "darwin":
        # v2.5+: real per-process attribution via the audio-detect Swift
        # helper. Pre-v2.5 this returned ``lambda: []`` because macOS
        # had no per-process API; that legacy stub was the reason
        # detection silently produced zero holders in the v2.5.0/v2.5.1
        # production builds even though platform_mac.get_mic_holders
        # was implemented correctly.
        from .platform_mac import get_mic_holders as fn
        return fn
    return lambda: []


def _default_is_mic_active():
    if sys.platform == "darwin":
        from .platform_mac import is_mic_active as fn
        return fn
    # Windows infers activeness from mic.holders being non-empty (cheaper
    # than a separate query); _snapshot_mic_state short-circuits to True
    # when holders is non-empty so this only fires when holders is empty.
    return lambda: False


def _default_get_running_processes():
    if sys.platform == "darwin":
        from .platform_mac import get_running_processes as fn
        return fn
    return lambda: frozenset()


def _default_get_foreground_info():
    if sys.platform == "win32":
        from .platform_win import get_foreground_info as fn
        return fn
    if sys.platform == "darwin":
        from .platform_mac import get_foreground_info as fn
        return fn
    return lambda: ForegroundInfo()


def _default_get_browser_window_titles():
    if sys.platform == "win32":
        from .platform_win import get_browser_window_titles as fn
        return fn
    if sys.platform == "darwin":
        from .platform_mac import get_browser_window_titles as fn
        return fn
    return lambda: []


def _default_get_browser_window_urls():
    if sys.platform == "win32":
        from .platform_win import get_browser_window_urls as fn
        return fn
    if sys.platform == "darwin":
        from .platform_mac import get_browser_window_urls as fn
        return fn
    return lambda: []


def _default_resolve_pids_for_spec() -> Callable[[DetectorSpec], tuple[int, ...]]:
    """Per-OS default PID resolver.

    - Windows: ``MatchResult.target_pids`` is always filled from
      ``mic.holders`` during ``match_whitelist``, so this fallback returns
      empty. (If future paths need a Windows enumerator, slot it here.)
    - macOS: delegate to ``platform_mac.resolve_pids_for_spec`` which
      enumerates via psutil + NSWorkspace.
    """
    if sys.platform == "darwin":
        from .platform_mac import resolve_pids_for_spec as fn
        return fn
    return lambda spec: ()


def _human_duration(secs: float) -> str:
    if secs < 60:
        return f"{int(round(secs))} seconds"
    mins = int(round(secs / 60))
    if mins < 60:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    h = secs / 3600.0
    # 1h, 1.5h, 2h, 2.5h, ...
    rounded = round(h * 2) / 2
    if rounded == int(rounded):
        return f"{int(rounded)} hour{'s' if int(rounded) != 1 else ''}"
    return f"{rounded} hours"
