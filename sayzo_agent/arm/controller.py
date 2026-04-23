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
from typing import Callable, Literal, Optional

from ..config import ArmConfig
from ..conversation import ConversationDetector, SessionState
from ..models import SessionCloseReason
from ..notify import ConsentResult, DesktopNotifier, Notifier
from . import detectors as _d
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
    """

    source: ArmSource
    app_key: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class _Cooldowns:
    """Per-app cooldown timestamps (monotonic seconds). Key = app_key."""
    entries: dict[str, float] = field(default_factory=dict)

    def active(self, app_key: str, now: float) -> bool:
        until = self.entries.get(app_key, 0.0)
        return now < until

    def set(self, app_key: str, until_mono: float) -> None:
        self.entries[app_key] = until_mono


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
        # Optional platform query overrides (for tests).
        get_mic_holders: Optional[Callable[[], list[_d.MicHolder]]] = None,
        is_mic_active: Optional[Callable[[], bool]] = None,
        get_running_processes: Optional[Callable[[], frozenset[str]]] = None,
        get_foreground_info: Optional[Callable[[], ForegroundInfo]] = None,
        get_browser_window_titles: Optional[Callable[[], list[str]]] = None,
    ) -> None:
        self.cfg = cfg
        self.detector = detector
        self.mic = mic_capture
        self.sys = sys_capture
        self.vad_mic = vad_mic
        self.vad_sys = vad_sys
        self.notifier = notifier

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
                await self._arm_internal(ArmReason(source="hotkey"))
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
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        fut: asyncio.Future[None] = loop.create_future()
        self._hotkey_confirmation_yes = fut

        result = await asyncio.gather(
            self._ask_consent(
                "Start recording?",
                "Sayzo will capture this conversation so we can coach you on it.",
                "Yes, start", "Cancel",
                timeout_secs=self.cfg.hotkey_confirm_timeout_secs,
                default_on_timeout="no",
            ),
            self._wait_or_timeout(fut, self.cfg.hotkey_confirm_timeout_secs),
            return_exceptions=True,
        )
        self._hotkey_confirmation_yes = None
        toast_result, fast_path_ok = result
        if fast_path_ok is True or toast_result == "yes":
            await self._arm_internal(ArmReason(source="hotkey"))
        # else: "no" / "timeout" / exceptions → stay disarmed

    async def _disarm_with_confirm(self) -> None:
        """Show the stop-confirmation toast. On Yes / double-tap, disarm."""
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        fut: asyncio.Future[None] = loop.create_future()
        self._hotkey_confirmation_yes = fut

        result = await asyncio.gather(
            self._ask_consent(
                "Stop recording?",
                "We'll save what we've captured so far.",
                "Yes, stop", "Keep going",
                timeout_secs=self.cfg.hotkey_confirm_timeout_secs,
                default_on_timeout="no",  # default = Keep going
            ),
            self._wait_or_timeout(fut, self.cfg.hotkey_confirm_timeout_secs),
            return_exceptions=True,
        )
        self._hotkey_confirmation_yes = None
        toast_result, fast_path_ok = result
        if fast_path_ok is True or toast_result == "yes":
            await self._disarm_internal(SessionCloseReason.HOTKEY_END)
        # else: stay armed

    async def _wait_or_timeout(self, fut: asyncio.Future, timeout: float) -> bool:
        try:
            await asyncio.wait_for(fut, timeout=timeout)
            return True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False

    # ---- arm / disarm internals -------------------------------------------

    async def _arm_internal(self, reason: ArmReason) -> None:
        """Transition DISARMED → ARMED.

        Opens both capture streams, resets VAD + detector epoch, flips the
        armed_event, fires the post-arm guidance toast, and starts the
        background watchers.
        """
        if self.state == ArmState.ARMED:
            return
        try:
            await self.mic.start()
            await self.sys.start()
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
            self.notifier.notify(
                "Couldn't start capturing",
                "Sayzo couldn't access your microphone or speakers. "
                f"Try closing other recording apps, then press {self.current_hotkey} again.",
            )
            return

        # Fresh-start invariant: VAD counter + detector epoch rewind so the
        # next frame behaves as the first frame of a cold-started source.
        try:
            self.vad_mic.reset()
            self.vad_sys.reset()
            self.detector.reset_source_epochs()
        except Exception:
            log.exception("[arm] reset_source_epochs failed (non-fatal)")

        self.state = ArmState.ARMED
        self._reason = reason
        self.armed_event.set()
        log.info("[arm] ARMED (reason=%s app=%s)", reason.source, reason.app_key or "-")
        self._fire_state_change()

        # Post-arm guidance toast (non-interactive). Skipped when the
        # Notifications pane's "Sayzo is capturing" sub-toggle is off.
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
        """Transition ARMED → DISARMED. Closes any open session with
        ``close_reason``, stops streams, cancels background watchers."""
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

        try:
            await self.mic.stop()
        except Exception:
            log.exception("[arm] mic.stop failed")
        try:
            await self.sys.stop()
        except Exception:
            log.exception("[arm] sys.stop failed")

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
            last_debug_dump = 0.0
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
                # One INFO-level dump per minute while disarmed so the log
                # shows whether the watcher is actually seeing capture
                # sessions, foreground info, etc. Critical for diagnosing
                # "I'm in a call and no toast fired" in the field.
                now_mono = time.monotonic()
                if now_mono - last_debug_dump >= 60.0:
                    last_debug_dump = now_mono
                    holder_names = [h.process_name for h in mic.holders]
                    log.info(
                        "[arm] watcher poll: holders=%s fg_proc=%s is_browser=%s "
                        "browser_windows=%d",
                        holder_names or "[]", fg.process_name, fg.is_browser,
                        len(fg.browser_window_titles),
                    )
                match = _d.match_whitelist(self.cfg.detectors, fg, mic)
                if match is None:
                    last_match_key = None
                    continue
                now = time.monotonic()
                # One-shot INFO log per match transition so field debugging
                # doesn't require raising the global log level.
                if match.app_key != last_match_key:
                    log.info(
                        "[arm] whitelist matched %s (%s) via %s",
                        match.display_name, match.app_key, match.source,
                    )
                    last_match_key = match.app_key
                if self._cooldowns.active(match.app_key, now):
                    log.debug(
                        "[arm] whitelist match for %s suppressed by cooldown",
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
                        )
                    )
                else:
                    self._cooldowns.set(
                        match.app_key,
                        time.monotonic() + self.cfg.cooldown_after_decline_secs,
                    )
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

    def _snapshot_mic_state(self) -> MicState:
        holders = self._q_mic_holders() or []
        active = bool(self._q_is_mic_active())
        running = self._q_running_procs() or frozenset()
        return MicState(holders=list(holders), active=active, running_processes=running)

    def _snapshot_foreground(self) -> ForegroundInfo:
        """Foreground info enriched with all visible browser window titles.

        The platform ``get_foreground_info`` query only knows about the
        frontmost window; we layer ``browser_window_titles`` on top so the
        matcher can find a Meet / Teams / Zoom-web window even when the
        user Alt+Tabs to a non-browser.
        """
        fg = self._q_foreground()
        try:
            titles = self._q_browser_titles() or []
        except Exception:
            log.debug("[arm] browser-titles query failed", exc_info=True)
            titles = []
        if not titles:
            return fg
        # Dataclass is frozen — build a new instance with the titles merged in.
        from dataclasses import replace
        return replace(fg, browser_window_titles=tuple(titles))

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
