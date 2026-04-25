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

from ..config import ArmConfig, DetectorSpec
from ..conversation import ConversationDetector, SessionState
from ..models import SessionCloseReason
from ..notify import ConsentResult, DesktopNotifier, Notifier
from . import detectors as _d
from . import seen_apps as _seen_apps
from .detectors import ForegroundInfo, MicState
from .hotkey import HotkeySource


ArmSource = Literal["hotkey", "whitelist"]

log = logging.getLogger(__name__)


# Small fast-path window for double-tapping the hotkey to bypass confirmation.
_DOUBLE_TAP_SECS = 1.0


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
    """

    source: ArmSource
    app_key: Optional[str] = None
    display_name: Optional[str] = None
    target_pids: tuple[int, ...] = ()


@dataclass
class _Cooldowns:
    """Per-app suppression state for the whitelist watcher.

    Two independent mechanisms, both keyed by ``app_key``:

    - ``entries`` — timed cooldown. Suppress until a monotonic deadline.
      Used after a natural session close so we don't immediately re-prompt
      for the same app while it's still holding the mic.
    - ``declined_release_at`` — session-based suppression. When the user
      declines or ignores a consent toast, we record the app as declined
      and clear it only once the app releases the mic for
      ``decline_release_grace_secs`` continuous seconds. Leaving + rejoining
      a meeting counts as a new session, so a fresh prompt fires.

    ``active`` returns True if EITHER mechanism is currently suppressing
    the app; the watcher calls it before showing a toast.
    """
    entries: dict[str, float] = field(default_factory=dict)
    # Value = monotonic time when the current "not holding" streak started,
    # or None if the app is currently still holding the mic (streak hasn't
    # started yet). Presence of the key = declined state.
    declined_release_at: dict[str, Optional[float]] = field(default_factory=dict)

    def active(self, app_key: str, now: float) -> bool:
        if app_key in self.declined_release_at:
            return True
        until = self.entries.get(app_key, 0.0)
        return now < until

    def set(self, app_key: str, until_mono: float) -> None:
        """Set a timed cooldown until the given monotonic time."""
        self.entries[app_key] = until_mono

    def mark_declined(self, app_key: str) -> None:
        """Mark the app as declined. Cleared by ``tick_session`` once the
        app releases the mic for long enough."""
        # Start with None — no release streak yet (app is still holding).
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
    ) -> None:
        self.cfg = cfg
        self.detector = detector
        self.mic = mic_capture
        self.sys = sys_capture
        self.vad_mic = vad_mic
        self.vad_sys = vad_sys
        self.notifier = notifier
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
                await self._arm_internal(
                    ArmReason(
                        source="hotkey",
                        target_pids=self._resolve_hotkey_target_pids(),
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
            await self._arm_internal(
                ArmReason(
                    source="hotkey",
                    target_pids=self._resolve_hotkey_target_pids(),
                )
            )

    async def _disarm_with_confirm(self) -> None:
        """Show the stop-confirmation toast. On Yes / double-tap, disarm."""
        if await self._race_confirm_with_double_tap(
            "Stop recording?",
            "We'll save what we've captured so far.",
            "Yes, stop", "Keep going",  # default = Keep going
        ):
            await self._disarm_internal(SessionCloseReason.HOTKEY_END)

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

        toast_task = asyncio.ensure_future(self._ask_consent(
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

        # Fresh-start invariant: VAD counter + detector epoch rewind so the
        # next frame behaves as the first frame of a cold-started source.
        # Done before flipping armed_event so _consume doesn't process any
        # in-flight frame against stale VAD state.
        try:
            self.vad_mic.reset()
            self.vad_sys.reset()
            self.detector.reset_source_epochs()
        except Exception:
            log.exception("[arm] reset_source_epochs failed (non-fatal)")

        self.state = ArmState.ARMED
        self._reason = reason
        self.armed_event.set()
        scope_desc = (
            f"app(pids={','.join(str(p) for p in reason.target_pids)})"
            if reason.target_pids else "endpoint"
        )
        log.info(
            "[arm] ARMED (reason=%s app=%s scope=%s)",
            reason.source, reason.app_key or "-", scope_desc,
        )
        self._fire_state_change()

        try:
            await self.mic.start()
            await self._start_sys_capture(reason.target_pids)
        except Exception as exc:
            log.warning("[arm] capture start failed: %s", exc, exc_info=True)
            # Best-effort close whatever did open so we don't leak streams.
            try:
                await self.mic.stop()
            except Exception:
                pass
            try:
                await self.sys.stop()
            except Exception:
                pass
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
                self.detector.commit_close(now, close_reason)
        except Exception:
            log.exception("[arm] detector commit_close failed")

        for task in (self._checkin_task, self._meeting_ended_task):
            if task is not None and not task.done():
                task.cancel()
        self._checkin_task = None
        self._meeting_ended_task = None

        # Per-app cooldown so the whitelist watcher doesn't re-prompt for
        # the same app immediately after a natural session end.
        prev = self._reason
        if prev is not None and prev.app_key:
            now_mono = time.monotonic()
            self._cooldowns.set(
                prev.app_key,
                now_mono + self.cfg.cooldown_after_session_secs,
            )

        self.state = ArmState.DISARMED
        self._reason = None
        self.armed_event.clear()
        log.info("[arm] DISARMED (reason=%s)", close_reason.value)
        self._fire_state_change()

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
        result = await self._ask_consent(
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
                match = _d.match_whitelist(self.cfg.detectors, fg, mic)
                if match is None:
                    last_match_key = None
                    # Record any unmatched mic-holders so the Settings
                    # Meeting Apps pane can suggest them. Only writes the
                    # first time a key appears this session; browsers are
                    # skipped since those are handled by the Web tab.
                    self._record_unmatched_holders(mic, fg)
                    continue
                # One-shot INFO log per match transition so field debugging
                # doesn't require raising the global log level.
                if match.app_key != last_match_key:
                    log.info(
                        "[arm] whitelist matched %s (%s) via %s",
                        match.display_name, match.app_key, match.source,
                    )
                    last_match_key = match.app_key
                if self._cooldowns.active(match.app_key, now_mono):
                    log.debug(
                        "[arm] whitelist match for %s suppressed",
                        match.app_key,
                    )
                    continue
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
                    await self._arm_internal(
                        ArmReason(
                            source="whitelist",
                            app_key=match.app_key,
                            display_name=match.display_name,
                            target_pids=self._resolve_target_pids_for_match(match),
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
                result = await self._ask_consent(
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
        try:
            grace = 0.0
            snooze_until = 0.0
            while self.state == ArmState.ARMED and not self._stop.is_set():
                try:
                    await asyncio.sleep(self.cfg.poll_interval_secs)
                except asyncio.CancelledError:
                    return
                now = time.monotonic()
                if now < snooze_until:
                    continue
                try:
                    mic = self._snapshot_mic_state()
                    fg = self._snapshot_foreground()
                except Exception:
                    log.debug("[arm] meeting-ended snapshot failed", exc_info=True)
                    continue
                still = _d.arm_app_still_holding_mic(
                    reason.app_key, self.cfg.detectors, mic, fg
                )
                if still:
                    grace = 0.0
                    continue
                grace += self.cfg.poll_interval_secs
                if grace < self.cfg.whitelist_arm_release_grace_secs:
                    continue
                # Fire meeting-ended toast.
                name = reason.display_name or "your meeting app"
                result = await self._ask_consent(
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
                # "Keep going" → snooze; fresh grace counter next poll.
                grace = 0.0
                snooze_until = time.monotonic() + self.cfg.meeting_ended_snooze_secs
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[arm] meeting-ended watcher crashed")

    # ---- helpers ----------------------------------------------------------

    def _resolve_target_pids_for_match(self, match: _d.MatchResult) -> tuple[int, ...]:
        """Resolve the PID set to scope system-audio capture to.

        Prefers ``match.target_pids`` (filled on Windows from mic.holders).
        Falls back to the injected resolver (macOS → psutil + NSWorkspace)
        when the match was a mac path that couldn't attribute PIDs inline.
        Empty tuple ⇒ caller uses endpoint-wide capture.
        """
        if match.target_pids:
            return match.target_pids
        spec = next(
            (s for s in self.cfg.detectors if s.app_key == match.app_key),
            None,
        )
        if spec is None:
            return ()
        try:
            pids = self._q_resolve_pids_for_spec(spec) or ()
        except Exception:
            log.debug(
                "[arm] resolve_pids_for_spec(%s) failed — falling back to endpoint scope",
                match.app_key, exc_info=True,
            )
            return ()
        return tuple(pids)

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

    def _resolve_hotkey_target_pids(self) -> tuple[int, ...]:
        """Smart-guess which PIDs to scope capture to for a hotkey-triggered
        arm. The two platforms diverge because macOS has no per-process
        mic-holder attribution.

        **Windows** (``mic.holders`` has real PIDs):
          1. Any whitelisted apps among the mic-holders → PIDs of those
             holders only (ignores unknowns like Cortana / voice assistants).
          2. Otherwise, any unknown apps holding the mic → all of their
             PIDs (catches new meeting apps we haven't whitelisted yet).
          3. Nothing holding the mic → endpoint-wide fallback.

        **macOS** (no per-process mic attribution):
          1. Mic active + foreground matches a whitelisted spec → PIDs
             enumerated for that spec via the platform resolver.
          2. Mic active + foreground is an identifiable app → PIDs for
             that app (treats foreground as the likely meeting app).
          3. Mic not active, or foreground unknown → endpoint fallback.
        """
        try:
            mic = self._snapshot_mic_state()
            fg = self._snapshot_foreground()
        except Exception:
            log.debug("[arm] hotkey smart-guess snapshot failed", exc_info=True)
            return ()

        if sys.platform == "win32":
            return self._resolve_hotkey_target_pids_win(mic)
        if sys.platform == "darwin":
            return self._resolve_hotkey_target_pids_mac(mic, fg)
        return ()

    def _resolve_hotkey_target_pids_win(self, mic: MicState) -> tuple[int, ...]:
        if not mic.holders:
            return ()
        whitelisted_proc_names: set[str] = set()
        for spec in self.cfg.detectors:
            if spec.disabled or spec.is_browser:
                continue
            for name in spec.process_names:
                whitelisted_proc_names.add(name.lower())

        whitelisted_pids: set[int] = set()
        all_pids: set[int] = set()
        for h in mic.holders:
            if h.pid <= 0:
                continue
            all_pids.add(h.pid)
            if h.process_name.lower() in whitelisted_proc_names:
                whitelisted_pids.add(h.pid)

        if whitelisted_pids:
            log.info(
                "[arm] hotkey smart-guess (win): scoping to %d whitelisted mic-holder(s)",
                len(whitelisted_pids),
            )
            return tuple(sorted(whitelisted_pids))
        if all_pids:
            log.info(
                "[arm] hotkey smart-guess (win): scoping to %d unknown mic-holder(s)",
                len(all_pids),
            )
            return tuple(sorted(all_pids))
        return ()

    def _resolve_hotkey_target_pids_mac(
        self, mic: MicState, fg: ForegroundInfo,
    ) -> tuple[int, ...]:
        if not mic.active:
            return ()
        fg_proc = (fg.process_name or "").lower()
        fg_bundle = (fg.bundle_id or "").lower()
        if not fg_proc and not fg_bundle:
            return ()

        # Pass 1: foreground matches a whitelisted spec → use the real
        # resolver for that spec (enumerates all PIDs matching its
        # process_names + bundle_ids).
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
                    log.info(
                        "[arm] hotkey smart-guess (mac): scoping to "
                        "whitelisted %s via foreground (%d pids)",
                        spec.app_key, len(pids),
                    )
                    return pids
                # Resolver returned empty — fall through to Pass 2.
                break

        # Pass 2: foreground is some identifiable app; enumerate its PIDs
        # even though it isn't whitelisted (user explicitly hotkey'd, so we
        # believe them about intent).
        synthetic = DetectorSpec(
            app_key="__hotkey_fg__",
            display_name="(foreground)",
            process_names=[fg.process_name] if fg.process_name else [],
            bundle_ids=[fg.bundle_id] if fg.bundle_id else [],
            is_browser=fg.is_browser,
        )
        try:
            pids = tuple(self._q_resolve_pids_for_spec(synthetic) or ())
        except Exception:
            log.debug(
                "[arm] resolve_pids_for_spec(synthetic foreground) raised",
                exc_info=True,
            )
            pids = ()
        if pids:
            log.info(
                "[arm] hotkey smart-guess (mac): scoping to foreground "
                "(%d pids, bundle=%s)",
                len(pids), fg_bundle or fg_proc,
            )
        return pids

    def _snapshot_mic_state(self) -> MicState:
        holders = self._q_mic_holders() or []
        active = bool(self._q_is_mic_active())
        running = self._q_running_procs() or frozenset()
        return MicState(holders=list(holders), active=active, running_processes=running)

    def snapshot_mic_state(self) -> MicState:
        """Public mic-holder snapshot for the Settings Meeting Apps pane.

        The Settings GUI opens on a worker thread; calling this directly is
        safe — on Windows the query hops onto the dedicated COM executor,
        and macOS's CoreAudio bindings are thread-safe for a one-shot read.
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


# ---- platform query resolution ------------------------------------------


def _default_get_mic_holders():
    if sys.platform == "win32":
        from .platform_win import get_mic_holders as fn
        return fn
    # macOS can't attribute per-process; return empty always.
    return lambda: []


def _default_is_mic_active():
    if sys.platform == "darwin":
        from .platform_mac import is_mic_active as fn
        return fn
    # On Windows we infer activeness from holders being non-empty (cheaper
    # than a separate query). The matcher handles the "active + running"
    # proxy only when is_mic_active=True, so returning False on Win is fine.
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
