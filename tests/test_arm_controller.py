"""Tests for the ArmController state machine.

Drives the controller with:
- FakeMic / FakeSys: record start/stop calls, never touch OS audio.
- FakeVAD: tracks reset() calls.
- FakeNotifier: records fire-and-forget toasts; ``ask_consent`` is
  scripted per test.
- Real ConversationDetector: PENDING_CLOSE / commit / revert logic is
  still exercised end-to-end.
- Platform query overrides: inject synthetic MicState / ForegroundInfo.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional


from sayzo_agent.arm.controller import ArmController, ArmReason, ArmState
from sayzo_agent.arm.detectors import ForegroundInfo, MicHolder
from sayzo_agent.config import ArmConfig, ConversationConfig, DetectorSpec, default_detector_specs
from sayzo_agent.conversation import ConversationDetector, SessionState
from sayzo_agent.models import SessionCloseReason, SpeechSegment


# ---- fakes ------------------------------------------------------------


class FakeCapture:
    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.fail_next_start = False
        # Latest values passed to start() — lets tests assert that
        # whitelist auto-arm + per-app-beta PIDs reach the system
        # capture layer (last_target_pids) and that the resolved mic
        # device reaches the mic capture layer (last_device).
        self.last_target_pids: tuple[int, ...] = ()
        self.last_device: Optional[str] = None

    async def start(
        self,
        *,
        target_pids: tuple[int, ...] = (),
        device: Optional[str] = None,
    ) -> None:
        self.start_count += 1
        self.last_target_pids = tuple(target_pids)
        self.last_device = device
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("simulated capture failure")

    async def stop(self) -> None:
        self.stop_count += 1

    @property
    def is_open(self) -> bool:
        return self.start_count > self.stop_count


class FakeVAD:
    def __init__(self, source: str = "mic") -> None:
        self.source = source
        self.reset_count = 0
        self.flush_count = 0
        # Tests fill this with SpeechSegments to simulate an in-progress
        # VAD segment that would only emit via flush() — exercises the
        # close-time flush wiring without needing to feed real audio.
        self.pending_on_flush: list[SpeechSegment] = []

    def reset(self) -> None:
        self.reset_count += 1
        # Real SileroVAD.reset() drops the in-progress segment too —
        # mirror that so a test can verify flush MUST happen BEFORE
        # reset, not after.
        self.pending_on_flush = []

    def flush(self):
        self.flush_count += 1
        pending = self.pending_on_flush
        self.pending_on_flush = []
        for seg in pending:
            yield seg


class FakeNotifier:
    """Notifier stand-in. ``fire_and_forget`` records non-interactive toasts.
    ``consent_script`` is a list of responses (yes/no/timeout) consumed in
    order by ``ask_consent``; if exhausted we default to the provided
    ``default_on_timeout``."""

    def __init__(self) -> None:
        self.fire_and_forget: list[tuple[str, str]] = []
        self.consent_calls: list[dict[str, Any]] = []
        self.consent_script: list[str] = []

    def notify(self, title: str, body: str) -> None:
        self.fire_and_forget.append((title, body))

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: str = "no",
        supersede: bool = False,
    ) -> str:
        self.consent_calls.append({
            "title": title, "body": body,
            "yes": yes_label, "no": no_label,
            "timeout": timeout_secs,
            "default_on_timeout": default_on_timeout,
            "supersede": supersede,
        })
        if self.consent_script:
            return self.consent_script.pop(0)
        return default_on_timeout


# ---- fixtures ---------------------------------------------------------


def _make_controller(
    *,
    notifier: Optional[FakeNotifier] = None,
    mic_holders: Optional[list[MicHolder]] = None,
    mic_active: bool = False,
    running: Optional[frozenset[str]] = None,
    foreground: Optional[ForegroundInfo] = None,
    cfg_overrides: Optional[dict] = None,
    system_scope: str = "endpoint",
) -> tuple[ArmController, ConversationDetector, FakeCapture, FakeCapture, FakeVAD, FakeVAD, FakeNotifier]:
    cfg_kwargs = {
        "hotkey": "ctrl+alt+s",
        "poll_interval_secs": 0.01,
        "hotkey_confirm_timeout_secs": 0.1,
        "consent_toast_timeout_secs": 0.1,
        "end_toast_timeout_secs": 0.1,
        "checkin_toast_timeout_secs": 0.1,
        "meeting_ended_toast_timeout_secs": 0.1,
        "whitelist_arm_release_grace_secs": 0.03,
        "force_close_after_keep_going_secs": 0.05,
        "decline_release_grace_secs": 0.05,
        "long_meeting_checkin_marks_secs": [3600.0],
        "detectors": default_detector_specs(),
    }
    if cfg_overrides:
        cfg_kwargs.update(cfg_overrides)
    cfg = ArmConfig(**cfg_kwargs)

    conv_cfg = ConversationConfig(joint_silence_close_secs=1.0)
    detector = ConversationDetector(conv_cfg)
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD(source="mic")
    vad_s = FakeVAD(source="system")
    notifier = notifier or FakeNotifier()
    current_mic = list(mic_holders or [])

    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: current_mic,
        is_mic_active=lambda: mic_active,
        get_running_processes=lambda: running or frozenset(),
        get_foreground_info=lambda: foreground or ForegroundInfo(),
        # Inject empty browser-title / URL queries so _snapshot_foreground
        # doesn't fall through to the REAL platform queries and read the
        # test machine's actual Chrome tabs (non-hermetic) — and, on
        # Windows, pay the ~0.5 s UIAutomation enumeration. Mirrors
        # test_whitelist_arm_uses_resolver_when_match_has_no_pids. Now that
        # the watcher snapshots run off-loop (run_in_executor), that real
        # query would resolve concurrently and race the test's fixed sleep.
        get_browser_window_titles=lambda: [],
        get_browser_window_urls=lambda: [],
        system_scope_fn=lambda: system_scope,
    )
    return ctrl, detector, mic, sys_cap, vad_m, vad_s, notifier


# ---- arm / disarm core -----------------------------------------------


async def test_initial_state_is_disarmed_no_streams():
    ctrl, _, mic, sys_cap, *_ = _make_controller()
    assert ctrl.state == ArmState.DISARMED
    assert not ctrl.armed_event.is_set()
    assert mic.start_count == 0
    assert sys_cap.start_count == 0


async def test_hotkey_arm_confirmation_yes_opens_streams():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, vad_m, vad_s, _ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert ctrl.armed_event.is_set()
    assert mic.start_count == 1 and sys_cap.start_count == 1
    assert vad_m.reset_count == 1 and vad_s.reset_count == 1
    # Post-arm guidance toast fires on arm.
    assert any("Sayzo is capturing" in t for t, _ in notifier.fire_and_forget)

    # Cleanup bg tasks
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


class _FakeHudLauncher:
    """Minimal HudLauncher stub. Records show_pill / hide_pill calls and
    delegates the consent path to the notifier so the controller's
    ``ask_consent_pausing_pill`` branch works against a FakeNotifier.

    Only the methods the controller actually invokes are stubbed; the rest
    of HudLauncher's surface (set_audio_levels, etc) is unused by the arm
    code paths we exercise here.
    """

    def __init__(self, notifier: "FakeNotifier") -> None:
        self._notifier = notifier
        self.show_pill_calls: list[dict[str, Any]] = []
        self.hide_pill_calls: int = 0

    def show_pill(self, **kwargs: Any) -> bool:
        self.show_pill_calls.append(kwargs)
        return True

    def hide_pill(self) -> bool:
        self.hide_pill_calls += 1
        return True

    def set_pill_stop_callback(self, _cb: Any) -> None:
        pass

    def reset_given_up(self) -> None:
        # v3.14: the arm path calls this to recover a given-up HUD; no-op
        # for the stub (it never enters the give-up state).
        pass

    def ask_consent_pausing_pill(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: str = "no",
        supersede: bool = False,
    ) -> str:
        # Delegate to the notifier so the existing FakeNotifier
        # ``consent_script`` mechanism drives this path too.
        return self._notifier.ask_consent(
            title, body, yes_label, no_label, timeout_secs,
            default_on_timeout, supersede=supersede,
        )


def _build_controller_with_indicator(
    *, indicator_visible: bool,
) -> tuple[ArmController, "FakeNotifier", "_FakeHudLauncher"]:
    """Construct an ArmController wired with a fake HUD launcher and the
    given show_recording_indicator preference."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    launcher = _FakeHudLauncher(notifier)
    notifier.launcher = launcher  # type: ignore[attr-defined]

    arm_cfg = ArmConfig(
        hotkey="ctrl+alt+s",
        poll_interval_secs=0.01,
        hotkey_confirm_timeout_secs=0.1,
        consent_toast_timeout_secs=0.1,
        end_toast_timeout_secs=0.1,
        checkin_toast_timeout_secs=0.1,
        meeting_ended_toast_timeout_secs=0.1,
        whitelist_arm_release_grace_secs=0.03,
        force_close_after_keep_going_secs=0.05,
        decline_release_grace_secs=0.05,
        long_meeting_checkin_marks_secs=[3600.0],
        detectors=default_detector_specs(),
    )
    conv_cfg = ConversationConfig(joint_silence_close_secs=1.0)
    detector = ConversationDetector(conv_cfg)
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD(source="mic")
    vad_s = FakeVAD(source="system")
    ctrl = ArmController(
        arm_cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: [],
        is_mic_active=lambda: False,
        get_running_processes=lambda: frozenset(),
        get_foreground_info=lambda: ForegroundInfo(),
        system_scope_fn=lambda: "endpoint",
        show_recording_indicator_fn=lambda: indicator_visible,
    )
    return ctrl, notifier, launcher


async def test_hotkey_arm_with_indicator_visible_shows_pill():
    """show_recording_indicator_fn returns True (default for new users +
    upgraders) → show_pill is called on arm."""
    ctrl, _, launcher = _build_controller_with_indicator(indicator_visible=True)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert len(launcher.show_pill_calls) == 1
    assert launcher.show_pill_calls[0]["reason"] == "hotkey"

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)
    # hide_pill always fires on disarm, even when no pill was ever shown.
    assert launcher.hide_pill_calls == 1


async def test_hotkey_arm_with_indicator_hidden_skips_pill():
    """show_recording_indicator_fn returns False ("Stay out of the way"
    chosen during onboarding) → show_pill is NOT called on arm. Capture
    still proceeds; hide_pill on disarm still fires (no-op against an
    already-absent pill on the React side)."""
    ctrl, _, launcher = _build_controller_with_indicator(indicator_visible=False)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert launcher.show_pill_calls == []

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)
    assert launcher.hide_pill_calls == 1


async def test_hotkey_arm_confirmation_no_keeps_disarmed():
    notifier = FakeNotifier()
    notifier.consent_script = ["no"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.DISARMED
    assert mic.start_count == 0 and sys_cap.start_count == 0


async def test_hotkey_arm_confirmation_timeout_keeps_disarmed():
    notifier = FakeNotifier()
    notifier.consent_script = ["timeout"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.DISARMED
    assert mic.start_count == 0 and sys_cap.start_count == 0


async def test_hotkey_disarm_confirmation_yes_closes_streams():
    notifier = FakeNotifier()
    # First press: arm (yes). Second press: disarm (yes).
    notifier.consent_script = ["yes", "yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.DISARMED
    assert mic.stop_count == 1 and sys_cap.stop_count == 1


async def test_hotkey_disarm_timeout_defaults_to_keep_going():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "timeout"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()  # arm
    await ctrl._on_hotkey_pressed()  # stop-confirm times out
    assert ctrl.state == ArmState.ARMED
    assert mic.stop_count == 0

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_stream_start_failure_notifies_and_stays_disarmed():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)
    mic.fail_next_start = True

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.DISARMED
    assert any("Couldn't start" in t for t, _ in notifier.fire_and_forget)


async def test_arm_cycle_preserves_mono_timestamp_through_to_session():
    """The seam between VAD-emits-monotonic-time and detector-rebases-to-
    session-relative must produce correct session-relative segments.

    Drive ArmController, set FakeVAD's pending segments with KNOWN monotonic
    timestamps (relative to arm time), hotkey-stop, retrieve the closed
    session, assert the rebased start_ts/end_ts match expectations within
    one frame's tolerance.
    """
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]
    ctrl, detector, _mic, _sys, vad_m, _vad_s, _ = _make_controller(notifier=notifier)

    arm_before = time.monotonic()
    await ctrl._on_hotkey_pressed()
    arm_after = time.monotonic()
    assert ctrl.state == ArmState.ARMED
    # session_t0_mono was anchored to whichever monotonic time the
    # controller captured during _arm_internal — must fall in the
    # arm_before..arm_after window.
    session_t0 = detector._session_t0_mono
    assert arm_before <= session_t0 <= arm_after

    # VAD emits segments with monotonic timestamps (not session-relative).
    # Pick mono times 1.0 and 5.5 seconds AFTER session_t0 so the rebase
    # in detector.on_segment lands at (1.0, 5.5) session-relative.
    expected_start_rel = 1.0
    expected_end_rel = 5.5
    vad_m.pending_on_flush = [
        SpeechSegment("mic", session_t0 + expected_start_rel, session_t0 + expected_end_rel),
    ]

    await ctrl._on_hotkey_pressed()  # disarm confirmation "yes"
    assert ctrl.state == ArmState.DISARMED

    closed = detector.take_closed_session()
    assert closed is not None
    assert len(closed.mic_segments) == 1
    seg = closed.mic_segments[0]
    assert abs(seg.start_ts - expected_start_rel) < 0.001, (
        f"rebased start_ts {seg.start_ts} != {expected_start_rel}"
    )
    assert abs(seg.end_ts - expected_end_rel) < 0.001, (
        f"rebased end_ts {seg.end_ts} != {expected_end_rel}"
    )


async def test_hotkey_stop_flushes_vad_pending_segments_into_session():
    """Regression: when the user hotkey-stops mid-utterance, the still-open
    VAD segment must be flushed into the closed session, not dropped.

    The armed-only model closes sessions abruptly on hotkey_end /
    check-in wrap-up / meeting-ended / joint-silence-confirmed.
    SileroVAD.feed() only emits a closed SpeechSegment after
    ``hangover_ms`` (300 ms) of unvoiced chunks; an abrupt close
    never delivers them, so without a flush hook the in-progress
    segment would be discarded when the next arm calls
    ``vad.reset()``. This was a real symptom in 2026-05-14 logs
    where a user with 15+ s of continuous speech saw their longest
    turn reported as 7.5 s — the longest turn was the one still
    open when they pressed hotkey.
    """
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # arm, disarm
    ctrl, detector, _mic, _sys, vad_m, vad_s, _ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED

    # Simulate VAD holding in-progress segments that only flush() can
    # surface — mirrors SileroVAD with _in_speech=True at the moment
    # the close hits.
    vad_m.pending_on_flush = [SpeechSegment("mic", 0.0, 5.0)]
    vad_s.pending_on_flush = [SpeechSegment("system", 1.0, 4.0)]

    await ctrl._on_hotkey_pressed()  # disarm confirmation "yes"
    assert ctrl.state == ArmState.DISARMED

    # Both VADs flushed exactly once, BEFORE the session committed.
    assert vad_m.flush_count == 1
    assert vad_s.flush_count == 1

    # The flushed segments landed in the now-closed session's segment
    # lists. Without the flush hook, both lists would be empty and the
    # closed session would carry no captured speech at all.
    closed = detector.take_closed_session()
    assert closed is not None
    assert len(closed.mic_segments) == 1, "mic flush segment dropped"
    assert len(closed.sys_segments) == 1, "system flush segment dropped"


async def test_arm_opens_detector_session_immediately():
    """Regression for v2.1.5 stale-frame bug: ``_arm_internal`` must open
    the detector's session at arm time (not wait for the first VAD
    segment) and frames flow directly into mic_pcm. Without this, stale
    frames left in mic.queue from a previous arm cycle would feed into
    the pre-buffer and the detector's gap-fill would inject ~200 s of
    zeros into the next session."""
    from sayzo_agent.conversation import SessionState
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, detector, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert detector.state == SessionState.OPEN
    assert detector._buffers is not None
    # Buffers start empty — no pre-buffer indirection.
    assert len(detector._buffers.mic_pcm) == 0
    assert len(detector._buffers.sys_pcm) == 0

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


# ---- pending-close flow (detector integration) -----------------------


async def test_pending_close_end_confirmation_yes_commits_and_disarms():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # arm, end-confirm Yes
    ctrl, detector, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    detector.on_segment(SpeechSegment("mic", 0.0, 1.0), now=100.0)
    detector.on_segment(SpeechSegment("system", 1.0, 2.0), now=101.0)
    detector.tick(110.0)  # joint silence crossed → PENDING_CLOSE
    # Let the end-confirmation coroutine run.
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.DISARMED
    assert mic.stop_count == 1


async def test_pending_close_end_confirmation_not_yet_reverts():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "no"]  # arm, end-confirm Not yet
    ctrl, detector, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    detector.on_segment(SpeechSegment("mic", 0.0, 1.0), now=100.0)
    detector.on_segment(SpeechSegment("system", 1.0, 2.0), now=101.0)
    detector.tick(110.0)
    await asyncio.sleep(0.05)
    # Stays armed; detector reverted to OPEN.
    assert ctrl.state == ArmState.ARMED
    assert mic.stop_count == 0

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_pending_close_timeout_commits_and_disarms():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "timeout"]
    ctrl, detector, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    detector.on_segment(SpeechSegment("mic", 0.0, 1.0), now=100.0)
    detector.on_segment(SpeechSegment("system", 1.0, 2.0), now=101.0)
    detector.tick(110.0)
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.DISARMED


async def test_pending_close_auto_revert_on_speech_skips_confirmation():
    """If a VAD segment arrives during the end-confirmation toast window,
    the detector auto-reverts; the arm controller sees OPEN and drops the
    toast result without disarming."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # would disarm, but shouldn't fire
    ctrl, detector, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl._on_hotkey_pressed()
    detector.on_segment(SpeechSegment("mic", 0.0, 1.0), now=100.0)
    detector.on_segment(SpeechSegment("system", 1.0, 2.0), now=101.0)
    detector.tick(110.0)  # PENDING_CLOSE, toast fires
    # Simulate user resuming speech BEFORE the toast callback runs.
    detector.on_segment(SpeechSegment("mic", 20.0, 22.0), now=122.0)
    await asyncio.sleep(0.05)
    # Still armed; detector back to OPEN.
    assert ctrl.state == ArmState.ARMED
    assert mic.stop_count == 0

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


# ---- pending-close defer gate (whitelist early-join) -----------------
#
# Regression guard for the early-join bug: a whitelist arm where the user
# joined the meeting early (no one talking yet) hit 45 s joint silence, the
# end-confirmation toast timed out (user wasn't watching), the session
# closed AND the app was suppressed for the rest of the meeting — so the
# real meeting that started minutes later was never re-prompted/captured.
# Fix: while the meeting app still holds the mic, joint silence must NOT
# close the session (the meeting-ended watcher owns the close on mic
# release). The gmeet path is the one that actually bit (browser specs go
# through `_browser_holds_mic`), so the defer test arms gmeet, not a
# desktop app.


def _make_gmeet_controller(
    notifier: FakeNotifier, holders: list[MicHolder],
    *, cfg_overrides: Optional[dict] = None,
) -> tuple[ArmController, ConversationDetector, FakeCapture, FakeCapture]:
    """Controller wired for a Windows-style Google Meet (browser) whitelist
    arm. ``holders`` is a mutable list the test reassigns in-place to
    simulate the meeting app releasing the mic. The gmeet match needs a
    browser process holding the mic + a meet.google.com foreground tab; the
    browser-title/URL queries are stubbed empty so the test never picks up
    the dev machine's real browser tabs (mirrors the manual construction in
    ``test_whitelist_arm_uses_resolver_when_match_has_no_pids``)."""
    cfg_kwargs = dict(
        hotkey="ctrl+alt+s",
        poll_interval_secs=0.01,
        hotkey_confirm_timeout_secs=0.1,
        consent_toast_timeout_secs=0.1,
        end_toast_timeout_secs=0.1,
        checkin_toast_timeout_secs=0.1,
        meeting_ended_toast_timeout_secs=0.1,
        whitelist_arm_release_grace_secs=0.03,
        force_close_after_keep_going_secs=0.05,
        decline_release_grace_secs=0.05,
        long_meeting_checkin_marks_secs=[3600.0],
        detectors=default_detector_specs(),
    )
    if cfg_overrides:
        cfg_kwargs.update(cfg_overrides)
    cfg = ArmConfig(**cfg_kwargs)
    detector = ConversationDetector(ConversationConfig(joint_silence_close_secs=1.0))
    mic = FakeCapture()
    sys_cap = FakeCapture()
    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=FakeVAD(source="mic"), vad_sys=FakeVAD(source="system"),
        notifier=notifier,
        get_mic_holders=lambda: list(holders),
        is_mic_active=lambda: True,
        get_running_processes=lambda: frozenset(),
        get_foreground_info=lambda: ForegroundInfo(
            process_name="chrome.exe",
            is_browser=True,
            browser_tab_url="https://meet.google.com/abc-defg-hij",
            browser_tab_title="Meet - Standup - Google Chrome",
        ),
        get_browser_window_titles=lambda: [],
        get_browser_window_urls=lambda: [],
    )
    return ctrl, detector, mic, sys_cap


async def _arm_gmeet(ctrl: ArmController) -> None:
    """Arm a gmeet whitelist session via the watcher, then cancel the
    background watchers so only ``_handle_pending_close`` is exercised (the
    meeting-ended / check-in tasks would otherwise race the joint-silence
    path under test)."""
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED, "expected gmeet whitelist arm"
    assert ctrl._reason is not None and ctrl._reason.app_key == "gmeet"
    for t in (ctrl._whitelist_task, ctrl._meeting_ended_task, ctrl._checkin_task):
        if t is not None:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


def _drive_joint_silence(detector: ConversationDetector) -> None:
    """One short system blip then 2 s of silence (close threshold is 1 s in
    the test cfg) → OPEN → PENDING_CLOSE → schedules _handle_pending_close.
    Mirrors the real early-join trigger (a 0.2 s join sound set the anchor)."""
    detector.on_segment(SpeechSegment("system", 0.0, 0.2), now=100.0)
    detector.tick(102.0)


async def test_whitelist_pending_close_defers_while_browser_holds_mic():
    """Core regression: gmeet armed, Chrome still holds the mic, joint
    silence fires → the session must NOT close, NO end-confirmation toast,
    and crucially the app must NOT be suppressed."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]  # arm only; no end-confirmation expected
    holders = [MicHolder("chrome.exe", 4344)]
    ctrl, detector, mic, _ = _make_gmeet_controller(notifier, holders)
    await _arm_gmeet(ctrl)

    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)

    assert ctrl.state == ArmState.ARMED
    assert detector.state == SessionState.OPEN  # reverted, still capturing
    assert mic.stop_count == 0
    assert not any(
        c["title"] == "Was that the end of your meeting?"
        for c in notifier.consent_calls
    )
    # The bug was suppression-on-timeout; gmeet must stay re-promptable.
    assert ctrl._cooldowns.suppressed_keys(time.monotonic()) == frozenset()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_pending_close_defers_on_failed_mic_snapshot():
    """Fail-safe: a COM-timeout returns empty holders (indistinguishable
    from a real release). The defer gate must keep the session open on an
    unreadable poll rather than closing+suppressing."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    holders = [MicHolder("chrome.exe", 4344)]
    ctrl, detector, mic, _ = _make_gmeet_controller(notifier, holders)
    await _arm_gmeet(ctrl)

    # Simulate the enumeration failing for this poll (empty + inactive).
    ctrl._q_mic_holders = lambda: []
    ctrl._q_is_mic_active = lambda: False

    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)

    assert ctrl.state == ArmState.ARMED
    assert detector.state == SessionState.OPEN
    assert mic.stop_count == 0
    assert ctrl._cooldowns.suppressed_keys(time.monotonic()) == frozenset()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_pending_close_closes_when_app_released():
    """When the arm-app is genuinely gone (a different, non-browser app is
    confirmed holding the mic), the defer gate falls through and joint
    silence closes the session as before — the backstop is preserved."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # arm, then end-confirmation Yes
    holders = [MicHolder("chrome.exe", 4344)]
    ctrl, detector, mic, _ = _make_gmeet_controller(notifier, holders)
    await _arm_gmeet(ctrl)

    # gmeet released the mic; some other (non-browser) app holds it now, so
    # `_browser_holds_mic` is False → arm-app confirmed gone → close proceeds.
    holders[:] = [MicHolder("someother.exe", 7)]

    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)

    assert ctrl.state == ArmState.DISARMED
    assert any(
        c["title"] == "Was that the end of your meeting?"
        for c in notifier.consent_calls
    )


async def test_hotkey_pending_close_closes_even_if_browser_holds_mic():
    """Scope guard: the defer gate is whitelist-only. A hotkey arm closes on
    joint silence as before even with a browser holding the mic (it has no
    arm-app to track, and a hotkey close doesn't suppress anything)."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # hotkey arm, end-confirmation Yes
    ctrl, detector, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("chrome.exe", 4344)],
        mic_active=True,
    )

    await ctrl._on_hotkey_pressed()  # source="hotkey", app_key=None
    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)

    assert ctrl.state == ArmState.DISARMED  # gate skipped (not a whitelist arm)


async def test_whitelist_deferred_session_still_closed_by_watcher_on_release():
    """Safety net (integration): a deferred early-join session is still
    bounded. Joint silence defers while Chrome holds the mic, then when the
    meeting genuinely ends (Chrome releases the mic) the meeting-ended
    watcher closes it. Validates the fix's central claim — "the
    meeting-ended watcher owns the close" — with the watcher actually
    running (the other defer tests cancel it for isolation)."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # arm; then meeting-ended "Wrap up"
    holders = [MicHolder("chrome.exe", 4344)]
    ctrl, detector, mic, _ = _make_gmeet_controller(notifier, holders)

    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None and ctrl._reason.app_key == "gmeet"
    # Stop ONLY the whitelist watcher; keep the meeting-ended watcher running.
    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass

    # Joint silence while Chrome still holds the mic → defers, stays armed.
    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.ARMED
    assert detector.state == SessionState.OPEN

    # Meeting really ends: Chrome releases the mic → meeting-ended watcher
    # (grace 0.03s) fires and closes the session.
    holders[:] = []
    await asyncio.sleep(0.2)
    assert ctrl.state == ArmState.DISARMED
    assert any(
        c["title"] == "Looks like your meeting ended"
        for c in notifier.consent_calls
    )


async def test_whitelist_abandoned_idle_session_closes_without_suppression():
    """Abandon cap: a session the app holds open with ~zero speech past
    early_join_abandon_secs is closed+discarded (bounds buffered memory) —
    but NOT suppressed, so a meeting that actually starts later can still
    re-prompt. No end-confirmation toast either (it's an abandon, not a
    user-facing wrap-up)."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]  # arm only
    holders = [MicHolder("chrome.exe", 4344)]
    # abandon immediately once idle (no speech) — exercises the cap path.
    ctrl, detector, mic, _ = _make_gmeet_controller(
        notifier, holders, cfg_overrides={"early_join_abandon_secs": 0.0},
    )
    await _arm_gmeet(ctrl)

    # Joint silence with no speech captured + app still holding the mic →
    # open_session_idle True (age>=0, ~0 voiced) → abandon.
    _drive_joint_silence(detector)
    await asyncio.sleep(0.05)

    assert ctrl.state == ArmState.DISARMED
    # Closed to bound memory, but the app was NOT marked declined.
    assert ctrl._cooldowns.suppressed_keys(time.monotonic()) == frozenset()
    # Abandon is silent — no end-confirmation toast.
    assert not any(
        c["title"] == "Was that the end of your meeting?"
        for c in notifier.consent_calls
    )


# ---- whitelist consent -----------------------------------------------


async def test_whitelist_match_fires_consent_and_arms_on_yes():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    # Drive one whitelist-watcher iteration manually by waking the task.
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    # Let it poll + toast + arm.
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None and ctrl._reason.app_key == "zoom"
    # v1.7.0: whitelist arm should scope system-audio capture to Zoom's PID.
    assert ctrl._reason.target_pids == (1234,)
    assert sys_cap.last_target_pids == (1234,)

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_arm_uses_resolver_when_match_has_no_pids():
    """Browser path: a Mac browser holder may have a PID for the audio
    helper but no whitelisted browser PIDs in mic.holders directly; the
    matcher returns ``target_pids=()`` and the controller must fall back
    to the injected resolver (``resolve_pids_for_spec``) to scope the
    system-audio capture.

    Pre-v2.5 this exercised the now-deleted macOS proxy path
    (``mic_active_plus_running``); v2.5+ exercises the equivalent
    browser-spec path where MatchResult.target_pids can still be empty
    because ``_pids_for_browser_holders`` only returns Windows-style
    pycaw PIDs."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]

    resolver_calls: list[str] = []

    def fake_resolver(spec) -> tuple[int, ...]:
        resolver_calls.append(spec.app_key)
        if spec.app_key == "gmeet":
            return (7777, 8888)
        return ()

    cfg = ArmConfig(
        hotkey="ctrl+alt+s",
        poll_interval_secs=0.01,
        hotkey_confirm_timeout_secs=0.1,
        consent_toast_timeout_secs=0.1,
        end_toast_timeout_secs=0.1,
        checkin_toast_timeout_secs=0.1,
        meeting_ended_toast_timeout_secs=0.1,
        whitelist_arm_release_grace_secs=0.03,
        force_close_after_keep_going_secs=0.05,
        decline_release_grace_secs=0.05,
        long_meeting_checkin_marks_secs=[3600.0],
        detectors=default_detector_specs(),
    )
    conv_cfg = ConversationConfig(joint_silence_close_secs=1.0)
    detector = ConversationDetector(conv_cfg)
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD(source="mic")
    vad_s = FakeVAD(source="system")

    # Simulate macOS browser path: a Chrome holder with bundle id but a
    # helper PID (so _pids_for_browser_holders returns ()), plus a Meet
    # tab title. The matcher returns target_pids=() and the controller
    # invokes the resolver for the gmeet spec to get the real PIDs.
    holder = MicHolder(
        process_name="com.google.Chrome",
        pid=0,  # 0 → excluded from _pids_for_browser_holders
        bundle_id="com.google.Chrome",
    )

    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: [holder],
        is_mic_active=lambda: True,
        get_running_processes=lambda: frozenset(),
        get_foreground_info=lambda: ForegroundInfo(
            process_name="Notes",
            bundle_id="com.apple.TextEdit",
            is_browser=False,
            browser_window_titles=("Meet - abc-defg-hij - Google Chrome",),
        ),
        # Inject empty browser-title / URL queries so the controller's
        # _snapshot_foreground enrichment doesn't fall back to the real
        # platform queries and pick up actual browser tabs from
        # whoever's running these tests on a workstation. Without this,
        # the injected browser_window_titles above would be overwritten.
        get_browser_window_titles=lambda: [],
        get_browser_window_urls=lambda: [],
        resolve_pids_for_spec=fake_resolver,
    )

    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None and ctrl._reason.app_key == "gmeet"
    assert ctrl._reason.target_pids == (7777, 8888)
    assert sys_cap.last_target_pids == (7777, 8888)
    assert "gmeet" in resolver_calls

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


# ---- mic-device routing (v2.7.12) ------------------------------------
#
# Verifies the fix for multi-mic users: a meeting app that picked a
# non-default capture device is detected (via the all-endpoints Windows
# enumeration + per-process device list from the macOS Swift helper)
# AND we record from the device the app is actually using, not the OS
# default. Both halves run through ``MicCapture.start(device=...)``.


async def test_whitelist_arm_routes_mic_to_holder_device():
    """Whitelist match where the matching holder reports a device_name →
    controller threads it into MicCapture.start. Without this fix, a user
    on a USB headset would be recorded from their built-in mic (faint,
    distant audio, not silent — worse than silent because captures look
    fine until you listen)."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[
            MicHolder(
                process_name="zoom.exe",
                pid=1234,
                device_name="Microphone (USB Headset)",
            ),
        ],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None
    assert ctrl._reason.mic_device == "Microphone (USB Headset)"
    assert mic.last_device == "Microphone (USB Headset)"

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_arm_default_mic_when_holder_has_no_device():
    """No regression: a holder with ``device_name=None`` (Windows
    endpoint that failed friendly-name read, or older macOS Swift
    binary without device reporting) → mic.start receives ``device=None``
    → sounddevice resolves to the OS default."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],  # No device_name.
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None
    assert ctrl._reason.mic_device is None
    assert mic.last_device is None

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_routes_mic_to_unknown_holder_device(monkeypatch):
    """Windows hotkey (default endpoint scope) + unknown holder with a
    device_name: system loopback stays endpoint-wide BUT the mic follows
    the unknown holder's device. The user explicitly hotkey'd; recording
    from where the mic is actually being used beats recording the OS
    default pointed at empty air.
    """
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[
            MicHolder(
                process_name="newmeeting.exe",
                pid=5555,
                device_name="USB Microphone",
            ),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == ()
    assert mic.last_device == "USB Microphone"

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_default_endpoint_ignores_whitelisted_holder(monkeypatch):
    """Windows hotkey with the default endpoint scope: even when a
    whitelisted holder (Zoom) is present we DO NOT narrow scope — the
    user opted out of per-app capture, so the whole endpoint is what
    they get. Mic still routes to the whitelisted holder's device
    (opportunistic routing is independent of scope mode)."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[
            MicHolder(
                process_name="zoom.exe",
                pid=1234,
                device_name="USB Headset Microphone",
            ),
            MicHolder(
                process_name="cortana.exe",
                pid=9999,
                device_name="Internal Microphone Array",
            ),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == ()
    assert mic.last_device == "USB Headset Microphone"

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_beta_routes_to_whitelisted_holder_device(monkeypatch):
    """Windows hotkey + beta `system_scope=arm_app`: Zoom on a USB
    headset (whitelisted) + Cortana on built-in mic. We scope sys-
    loopback to Zoom AND route the mic to the USB headset (the
    whitelisted holder's device). Cortana's device must not win even
    though its holder was also present."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        system_scope="arm_app",
        mic_holders=[
            MicHolder(
                process_name="zoom.exe",
                pid=1234,
                device_name="USB Headset Microphone",
            ),
            MicHolder(
                process_name="cortana.exe",
                pid=9999,
                device_name="Internal Microphone Array",
            ),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == (1234,)
    assert mic.last_device == "USB Headset Microphone"

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_decline_suppresses_until_app_releases_mic():
    """Decline → session-based suppression: stays quiet while the app is
    still holding the mic; clears once the app releases for grace_secs."""
    notifier = FakeNotifier()
    # Only one "no" in the script — subsequent toasts would also auto-"no",
    # but the point is we should NOT see subsequent toasts for zoom while
    # zoom is still holding the mic.
    notifier.consent_script = ["no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.DISARMED
    # Declined → active suppression until the app releases the mic.
    assert ctrl._cooldowns.active("zoom") is True
    assert "zoom" in ctrl._cooldowns.declined_release_at
    # No second toast fired despite zoom still holding the mic for many polls.
    zoom_toasts = [c for c in notifier.consent_calls
                   if "Zoom" in c.get("body", "")]
    assert len(zoom_toasts) == 1

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


async def test_whitelist_decline_clears_when_app_releases_mic():
    """Decline → app releases mic → grace elapses → fresh toast fires on
    next match (new session)."""
    notifier = FakeNotifier()
    # First toast: user declines. Second toast (after re-acquire): user accepts.
    notifier.consent_script = ["no", "yes"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    # Let the first toast fire + decline land.
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.DISARMED
    assert "zoom" in ctrl._cooldowns.declined_release_at
    # Zoom releases the mic (user left the meeting).
    ctrl._q_mic_holders = lambda: []
    # Wait long enough for decline_release_grace_secs (0.05) to elapse
    # across a few polls.
    await asyncio.sleep(0.2)
    assert "zoom" not in ctrl._cooldowns.declined_release_at
    # Zoom re-acquires the mic (user joined a new meeting).
    ctrl._q_mic_holders = lambda: [MicHolder("zoom.exe", 5678)]
    # Let the second toast fire + accept land.
    await asyncio.sleep(0.2)
    assert ctrl.state == ArmState.ARMED

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_session_end_clears_when_app_releases_mic():
    """Bug 2026-04-28: arm via whitelist → end session → close + reopen
    the app. Pre-fix, a flat 10-min timed cooldown silently suppressed
    the next consent toast even though the app released the mic and
    re-acquired it (a new session from the user's perspective). Now
    post-session uses the same release-tracking as the decline path, so
    a fresh prompt fires the moment the app re-acquires the mic past
    the grace window."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # accept both whitelist toasts
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    # First match → toast → arm.
    await asyncio.sleep(0.1)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None and ctrl._reason.app_key == "zoom"

    # User stops the session via hotkey / tray.
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)
    assert ctrl.state == ArmState.DISARMED
    # Suppression is now release-based, not wall-clock — so it lives in
    # declined_release_at, not in some flat-timer dict.
    assert "zoom" in ctrl._cooldowns.declined_release_at

    # While zoom keeps holding the mic (e.g. user stayed in the meeting
    # but stopped recording), the watcher must NOT re-prompt.
    await asyncio.sleep(0.1)
    assert ctrl.state == ArmState.DISARMED
    zoom_toasts = [c for c in notifier.consent_calls
                   if "Zoom" in c.get("body", "")]
    assert len(zoom_toasts) == 1

    # Zoom releases the mic (user closed it). After grace_secs of
    # continuous absence, the suppression clears.
    ctrl._q_mic_holders = lambda: []
    await asyncio.sleep(0.2)
    assert "zoom" not in ctrl._cooldowns.declined_release_at

    # Zoom re-acquires the mic (a brand-new session). Toast fires.
    ctrl._q_mic_holders = lambda: [MicHolder("zoom.exe", 9999)]
    await asyncio.sleep(0.2)
    assert ctrl.state == ArmState.ARMED

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_decline_in_browser_does_not_mask_other_browser_match():
    """v1.8.2: a declined gmeet match in a background tab must NOT shadow
    a chatgpt-com match in the foreground tab. Pre-fix the watcher would
    find gmeet first (default detector order, matches via background
    browser_window_urls), see it's suppressed, and bail for the whole
    poll — leaving ChatGPT silently uncaptured. With ``exclude_app_keys``
    plumbed in, gmeet is skipped entirely and chatgpt-com fires its toast.
    """
    notifier = FakeNotifier()
    notifier.consent_script = ["no", "yes"]  # decline gmeet, accept chatgpt

    custom = DetectorSpec(
        app_key="chatgpt-com", display_name="ChatGPT", is_browser=True,
        url_patterns=[r"^https://chatgpt\.com/"],
    )

    fg_meet = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://meet.google.com/aaa-bbb-ccc",
    )
    ctrl, _, _, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("chrome.exe", 1111)],
        foreground=fg_meet,
        cfg_overrides={
            "detectors": default_detector_specs() + [custom],
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    # First toast fires for gmeet; user declines.
    await asyncio.sleep(0.05)
    assert "gmeet" in ctrl._cooldowns.declined_release_at
    assert ctrl.state == ArmState.DISARMED

    # User switches focus to the chatgpt tab; Meet still in background.
    fg_chatgpt = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://chatgpt.com/c/abc",
        browser_window_urls=(
            "https://chatgpt.com/c/abc",
            "https://meet.google.com/aaa-bbb-ccc",
        ),
    )
    ctrl._q_foreground = lambda: fg_chatgpt

    # Second toast must fire for chatgpt-com despite gmeet still matching
    # via background_window_urls.
    await asyncio.sleep(0.2)
    assert ctrl.state == ArmState.ARMED
    assert ctrl._reason is not None
    assert ctrl._reason.app_key == "chatgpt-com"

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_decline_stays_active_while_app_flaps():
    """Brief mic dips during a declined session (e.g. muted for a moment)
    must not prematurely clear the decline — the app has to be continuously
    off the mic for the full grace window."""
    notifier = FakeNotifier()
    notifier.consent_script = ["no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={
            "poll_interval_secs": 0.01,
            "decline_release_grace_secs": 0.15,  # longer than a flap
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.05)
    assert "zoom" in ctrl._cooldowns.declined_release_at
    # Brief "not holding" blip.
    ctrl._q_mic_holders = lambda: []
    await asyncio.sleep(0.05)
    # Re-acquire before grace elapses.
    ctrl._q_mic_holders = lambda: [MicHolder("zoom.exe", 1234)]
    await asyncio.sleep(0.1)
    # Still suppressed — the flap didn't count as a session end.
    assert "zoom" in ctrl._cooldowns.declined_release_at

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


# ---- consent-toast timeout re-ask (v3.18) ----------------------------


async def test_whitelist_consent_timeout_schedules_reask_then_arms():
    """v3.18: a consent-toast timeout is NOT a decline. It schedules one
    re-ask after consent_timeout_refire_delay_secs; the re-ask uses
    follow-up copy and a Yes on it arms normally."""
    notifier = FakeNotifier()
    notifier.consent_script = ["timeout", "yes"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={"consent_timeout_refire_delay_secs": 0.3},
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    # First toast fires and times out → snoozed, NOT declined.
    await asyncio.sleep(0.1)
    assert ctrl.state == ArmState.DISARMED
    assert len(notifier.consent_calls) == 1
    assert "zoom" in ctrl._cooldowns.timeout_snooze
    assert "zoom" not in ctrl._cooldowns.declined_release_at
    # Transport failure must route to the re-ask path, not auto-decline.
    assert notifier.consent_calls[0]["default_on_timeout"] == "timeout"
    assert "Looks like you're in Zoom" in notifier.consent_calls[0]["body"]

    # After the refire delay, the re-ask fires (zoom still holds the mic)
    # with follow-up copy, and Yes arms.
    await asyncio.sleep(0.6)
    assert len(notifier.consent_calls) == 2
    assert "Still in your Zoom meeting" in notifier.consent_calls[1]["body"]
    assert ctrl.state == ArmState.ARMED
    assert "zoom" not in ctrl._cooldowns.timeout_snooze

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_second_timeout_suppresses_for_rest_of_meeting():
    """Timeout budget is one re-ask per meeting: a second timeout flips
    the app to declined (no third toast while it keeps holding the mic)."""
    notifier = FakeNotifier()
    notifier.consent_script = ["timeout", "timeout"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={"consent_timeout_refire_delay_secs": 0.05},
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    await asyncio.sleep(0.4)
    assert ctrl.state == ArmState.DISARMED
    assert len(notifier.consent_calls) == 2
    assert "zoom" in ctrl._cooldowns.declined_release_at
    assert "zoom" not in ctrl._cooldowns.timeout_snooze

    # Still no third toast many polls later.
    await asyncio.sleep(0.2)
    assert len(notifier.consent_calls) == 2

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


async def test_whitelist_timeout_reask_dropped_when_meeting_ends():
    """Meeting ends during the snooze window → the re-ask never fires
    (matching requires a live mic-holder) and the snooze state clears via
    the release streak, so the NEXT meeting starts fresh with first-ask
    copy and a full budget."""
    notifier = FakeNotifier()
    notifier.consent_script = ["timeout", "yes"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        # Refire far beyond the test horizon — it must never fire here.
        cfg_overrides={"consent_timeout_refire_delay_secs": 10.0},
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    await asyncio.sleep(0.05)
    assert "zoom" in ctrl._cooldowns.timeout_snooze

    # Zoom releases the mic (meeting over) → snooze drops after the
    # release grace, with no re-ask ever fired.
    ctrl._q_mic_holders = lambda: []
    await asyncio.sleep(0.2)
    assert "zoom" not in ctrl._cooldowns.timeout_snooze
    assert "zoom" not in ctrl._cooldowns.declined_release_at
    assert len(notifier.consent_calls) == 1

    # A new meeting → fresh FIRST ask (not the follow-up copy) → arms.
    ctrl._q_mic_holders = lambda: [MicHolder("zoom.exe", 5678)]
    await asyncio.sleep(0.2)
    assert len(notifier.consent_calls) == 2
    assert "Looks like you're in Zoom" in notifier.consent_calls[1]["body"]
    assert ctrl.state == ArmState.ARMED

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_decline_on_reask_suppresses_and_drops_snooze():
    """Explicit "Not now" on the re-ask → declined for the rest of the
    meeting, pending snooze gone, no further toasts."""
    notifier = FakeNotifier()
    notifier.consent_script = ["timeout", "no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={"consent_timeout_refire_delay_secs": 0.05},
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())

    await asyncio.sleep(0.4)
    assert ctrl.state == ArmState.DISARMED
    assert len(notifier.consent_calls) == 2
    assert "zoom" in ctrl._cooldowns.declined_release_at
    assert "zoom" not in ctrl._cooldowns.timeout_snooze

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


def test_cooldowns_mark_timeout_zero_budget_declines_immediately():
    """max_refires=0 restores pre-v3.18 behavior: first timeout →
    declined, no snooze scheduled."""
    from sayzo_agent.arm.controller import _Cooldowns

    cd = _Cooldowns()
    snoozed = cd.mark_timeout(
        "zoom", 100.0, refire_delay_secs=120.0, max_refires=0,
    )
    assert snoozed is False
    assert "zoom" in cd.declined_release_at
    assert "zoom" not in cd.timeout_snooze


# ---- meeting-ended watcher ------------------------------------------


async def test_meeting_ended_fires_after_grace_period_and_commits_on_yes():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "yes"]  # whitelist-consent yes, then Wrap up yes
    ctrl, _, mic, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.ARMED

    # Simulate Zoom releasing the mic by mutating the injected query list.
    # The controller's injected callback returns the list by reference.
    # Easiest: replace the internal query.
    ctrl._q_mic_holders = lambda: []
    # Wait for grace + toast + disarm.
    await asyncio.sleep(0.25)
    assert ctrl.state == ArmState.DISARMED

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


async def test_meeting_ended_watcher_does_not_start_for_hotkey_arm():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, *_ = _make_controller(notifier=notifier)
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert ctrl._meeting_ended_task is None

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_meeting_ended_keep_going_force_closes_after_threshold():
    """After 'Keep going', the watcher does NOT re-toast — it silently
    accumulates consecutive mic-holder absence and force-closes once it
    crosses ``force_close_after_keep_going_secs``. An informational
    (non-consent) toast tells the user what happened so the close
    doesn't feel mysterious. Replaces the old snooze-and-refire flow."""
    notifier = FakeNotifier()
    # Whitelist yes → arm. First (and only) meeting-ended toast → "Keep going" (no).
    notifier.consent_script = ["yes", "no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={
            "poll_interval_secs": 0.01,
            "whitelist_arm_release_grace_secs": 0.02,
            "force_close_after_keep_going_secs": 0.06,
            "meeting_ended_toast_timeout_secs": 0.02,
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.ARMED
    ctrl._q_mic_holders = lambda: []
    # Wait for first toast → Keep going → 60 ms+ of continued absence → force-close.
    await asyncio.sleep(0.35)
    assert ctrl.state == ArmState.DISARMED
    # Exactly ONE meeting-ended consent toast — the second close path
    # is silent (informational notify, not consent).
    meeting_ended_calls = [c for c in notifier.consent_calls
                           if c["title"] == "Looks like your meeting ended"]
    assert len(meeting_ended_calls) == 1
    # Informational "we wrapped it up" toast fired (fire-and-forget,
    # captured by FakeNotifier.fire_and_forget).
    info_calls = [t for t in notifier.fire_and_forget
                  if "Wrapped up" in t[0]]
    assert len(info_calls) == 1, (
        f"expected one informational 'Wrapped up' toast, "
        f"got {[t[0] for t in notifier.fire_and_forget]}"
    )

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


async def test_meeting_ended_keep_going_resets_on_mic_return():
    """After 'Keep going', if the arm-app comes back into mic-holders
    the absence counter resets — we don't force-close on cumulative
    absence, only consecutive. Without this, brief mic-release blips
    after Keep going would still trigger force-close eventually.

    Force-close threshold is intentionally far larger than the test's
    real-time window so Windows asyncio scheduling jitter (sleep
    granularity ≈ 15 ms, run_in_executor thread-pool roundtrip) can't
    push absence over the threshold before the holder-restoration
    sleep returns. The test isn't validating that force-close fires —
    only that holder return resets the counter, so any value greater
    than the total wallclock budget keeps the assertion deterministic.
    """
    notifier = FakeNotifier()
    notifier.consent_script = ["yes", "no"]  # arm, then Keep going
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={
            "poll_interval_secs": 0.01,
            "whitelist_arm_release_grace_secs": 0.02,
            # Far larger than the test's wallclock window so jitter can't
            # trip force-close before the holder is restored.
            "force_close_after_keep_going_secs": 2.0,
            "meeting_ended_toast_timeout_secs": 0.02,
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.ARMED
    # Drop holders → first toast → Keep going.
    ctrl._q_mic_holders = lambda: []
    await asyncio.sleep(0.10)
    # Holder returns BEFORE the force-close threshold elapses. Counter resets.
    ctrl._q_mic_holders = lambda: [MicHolder("zoom.exe", 1234)]
    await asyncio.sleep(0.15)
    # Still ARMED — force-close didn't trip because absence wasn't consecutive.
    assert ctrl.state == ArmState.ARMED, (
        "mic-holder return after Keep going must reset the absence counter"
    )

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


# ---- hotkey scope resolution (v2.9+: endpoint default, beta narrows) ----
#
# Verifies the Windows and macOS branches of `_resolve_hotkey_arm`.
# Default scope (system_scope=endpoint) skips PID computation entirely;
# beta scope (system_scope=arm_app) runs the whitelisted-holder logic
# with an endpoint-fallback for unknown holders. On Windows we drive
# with synthetic mic.holders; on macOS we flip sys.platform and drive
# via foreground + mic.active + an injected resolver.


async def test_hotkey_win_beta_scopes_whitelisted_over_unknown(monkeypatch):
    """Windows hotkey + beta `arm_app`: Zoom + Cortana both holding mic
    → tap Zoom only. The whitelisted-first tier keeps the voice-
    assistant false-positive from contaminating the session."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        system_scope="arm_app",
        mic_holders=[
            MicHolder("zoom.exe", 1234),
            MicHolder("cortana.exe", 9999),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == (1234,)

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_default_endpoint_with_unknown_holders(monkeypatch):
    """Windows hotkey (default endpoint scope): mic-holders exist but
    none are whitelisted → endpoint scope.

    This is the v2.9+ shipping behavior — the hotkey path no longer
    computes PIDs at all when the per-app beta toggle is off. (Unknown
    holders are still recorded to seen_apps so the user can promote
    them for next time.)
    """
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[
            MicHolder("newmeeting.exe", 5555),
            MicHolder("oldvoipapp.exe", 6666),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    # Endpoint scope — empty tuple — is what SystemCapture turns into a
    # fall-through to endpoint-wide loopback on Windows.
    assert sys_cap.last_target_pids == ()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_beta_endpoint_fallback_when_no_whitelisted_holder(monkeypatch):
    """Windows hotkey + beta `arm_app`: unknown mic-holders only →
    endpoint fallback. This is the v2.7.9 safety-rule: when the toggle
    is on but the only mic-holders aren't whitelisted (Steam Voice,
    ChatGPT voice, Voice Recorder, …), scope to endpoint instead of
    those PIDs to avoid silent capture."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(
        notifier=notifier,
        system_scope="arm_app",
        mic_holders=[
            MicHolder("newmeeting.exe", 5555),
            MicHolder("oldvoipapp.exe", 6666),
        ],
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == ()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_win_endpoint_when_no_holders(monkeypatch):
    """Windows: nothing holds the mic → empty tuple → SystemCapture
    falls back to endpoint-wide loopback. Applies in both scope modes."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "win32")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == ()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_mac_beta_uses_whitelisted_resolver_on_match(monkeypatch):
    """macOS hotkey + beta `arm_app`: foreground matches a whitelisted
    spec → resolver populates PIDs. On Mac, mic.holders is empty so the
    rule keys off foreground + mic.active."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "darwin")

    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]

    def fake_resolver(spec):
        if spec.app_key == "zoom":
            return (3000, 3001)
        return ()

    cfg = ArmConfig(
        hotkey="ctrl+alt+s",
        poll_interval_secs=0.01,
        hotkey_confirm_timeout_secs=0.1,
        consent_toast_timeout_secs=0.1,
        end_toast_timeout_secs=0.1,
        checkin_toast_timeout_secs=0.1,
        meeting_ended_toast_timeout_secs=0.1,
        whitelist_arm_release_grace_secs=0.03,
        force_close_after_keep_going_secs=0.05,
        decline_release_grace_secs=0.05,
        long_meeting_checkin_marks_secs=[3600.0],
        detectors=default_detector_specs(),
    )
    conv_cfg = ConversationConfig(joint_silence_close_secs=1.0)
    detector = ConversationDetector(conv_cfg)
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD(source="mic")
    vad_s = FakeVAD(source="system")

    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: [],
        is_mic_active=lambda: True,
        get_running_processes=lambda: frozenset({"us.zoom.xos"}),
        get_foreground_info=lambda: ForegroundInfo(bundle_id="us.zoom.xos"),
        resolve_pids_for_spec=fake_resolver,
        system_scope_fn=lambda: "arm_app",
    )

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == (3000, 3001)

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_mac_endpoint_when_mic_inactive(monkeypatch):
    """macOS: mic not active → endpoint fallback, regardless of what's in
    the foreground or which scope mode is set."""
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "darwin")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, _, sys_cap, *_ = _make_controller(
        notifier=notifier,
        mic_active=False,
        foreground=ForegroundInfo(bundle_id="us.zoom.xos"),
    )
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    assert sys_cap.last_target_pids == ()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_hotkey_mac_beta_endpoint_when_foreground_not_whitelisted(monkeypatch):
    """macOS: mic active + foreground is some non-whitelisted app →
    endpoint scope (NOT scope to a synthetic spec for that foreground).

    The earlier behavior built a synthetic ``DetectorSpec`` for the
    foreground app and scoped the tap to its PIDs on the theory that
    "the user explicitly hotkey'd, so we believe them about intent."
    In practice that produced silent captures when the foreground at
    hotkey time wasn't the audio source the user wanted captured —
    classic case: the user has Notes / Slack / a browser foreground
    while a Zoom call runs in the background. The synthetic foreground
    spec captured Notes, which produces no audio, and the meeting was
    lost. Endpoint scope guarantees we capture the speaker output
    regardless of which window is forward.
    """
    monkeypatch.setattr("sayzo_agent.arm.controller.sys.platform", "darwin")
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]

    def fake_resolver(spec):
        # Defensive: if anything still routes through the resolver for
        # an unwhitelisted foreground, return PIDs so the test would
        # FAIL (catching a regression where Pass 2 sneaks back in).
        return (9001, 9002)

    cfg = ArmConfig(
        hotkey="ctrl+alt+s",
        poll_interval_secs=0.01,
        hotkey_confirm_timeout_secs=0.1,
        consent_toast_timeout_secs=0.1,
        end_toast_timeout_secs=0.1,
        checkin_toast_timeout_secs=0.1,
        meeting_ended_toast_timeout_secs=0.1,
        whitelist_arm_release_grace_secs=0.03,
        force_close_after_keep_going_secs=0.05,
        decline_release_grace_secs=0.05,
        long_meeting_checkin_marks_secs=[3600.0],
        detectors=default_detector_specs(),
    )
    conv_cfg = ConversationConfig(joint_silence_close_secs=1.0)
    detector = ConversationDetector(conv_cfg)
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD(source="mic")
    vad_s = FakeVAD(source="system")

    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: [],
        is_mic_active=lambda: True,
        get_running_processes=lambda: frozenset({"com.apple.notes"}),
        # Foreground = Apple Notes — definitely not in the whitelist.
        get_foreground_info=lambda: ForegroundInfo(bundle_id="com.apple.notes"),
        resolve_pids_for_spec=fake_resolver,
        system_scope_fn=lambda: "arm_app",
    )

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED
    # Endpoint scope — empty tuple — even with beta ON, because Notes
    # isn't whitelisted (the resolver would happily return PIDs for it
    # but we skip the resolver call for non-whitelisted foreground).
    assert sys_cap.last_target_pids == ()

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


# ---- rebind ---------------------------------------------------------


async def test_rebind_hotkey_updates_cfg_on_success(monkeypatch):
    """We can't register a real pynput hotkey in this test environment, so
    patch HotkeySource.register to succeed. Verifies rebind plumbing."""
    ctrl, _, *_ = _make_controller()
    # Stand up a fake HotkeySource that records rebind calls.
    class _FakeHotkey:
        def __init__(self) -> None:
            self.binding: Optional[str] = "ctrl+alt+s"
            self.rebind_calls: list[str] = []

        def rebind(self, new_binding: str):
            self.rebind_calls.append(new_binding)
            self.binding = new_binding
            return None  # success

        def unregister(self) -> None:
            pass

    ctrl._hotkey = _FakeHotkey()
    err = ctrl.rebind_hotkey("ctrl+alt+shift+r")
    assert err is None
    assert ctrl.cfg.hotkey == "ctrl+alt+shift+r"


# ---- tray click: no confirmation, reentrancy lock -------------------


async def test_tray_click_arms_without_confirmation_toast():
    """Tray menu click is deliberate (the label IS the action) — we must
    not fire a 'Start recording?' consent toast on top of it."""
    notifier = FakeNotifier()
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl.arm_from_tray()
    assert ctrl.state == ArmState.ARMED
    assert mic.start_count == 1 and sys_cap.start_count == 1
    # No consent toast was shown — only the non-interactive "Sayzo is
    # capturing" post-arm guidance counts as fire-and-forget.
    assert notifier.consent_calls == []

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_tray_click_disarms_without_confirmation_toast():
    """Counterpart to the arm case: clicking 'Stop recording' from the
    tray must stop immediately, not pop a 'Stop recording?' toast.

    The user's report was: they click Stop, a toast appears, the mic
    indicator stays on (because disarm hasn't actually happened yet),
    they click again to try to force it, and the state ends up flipped.
    This test pins the fix — tray disarm never asks.
    """
    notifier = FakeNotifier()
    ctrl, _, mic, sys_cap, *_ = _make_controller(notifier=notifier)

    await ctrl.arm_from_tray()  # arm
    consent_calls_before = len(notifier.consent_calls)
    await ctrl.arm_from_tray()  # disarm
    assert ctrl.state == ArmState.DISARMED
    assert mic.stop_count == 1 and sys_cap.stop_count == 1
    # Disarm path added zero new consent toasts.
    assert len(notifier.consent_calls) == consent_calls_before


async def test_tray_click_drops_concurrent_second_click():
    """Rapid double-click on the tray menu should not stack two
    transitions. While the first is mid-await (mic stream opening), the
    second click must be dropped — queuing would produce the classic
    'I clicked stop and it armed again' flip-flop the user reported."""
    ctrl, _, mic, sys_cap, *_ = _make_controller()

    # Gate mic.start so the first click is parked mid-transition while
    # we fire the second click.
    gate = asyncio.Event()
    original_start = mic.start

    async def slow_start() -> None:
        await gate.wait()
        await original_start()

    mic.start = slow_start  # type: ignore[assignment]

    task1 = asyncio.create_task(ctrl.arm_from_tray())
    # Let task1 begin and park at the mic.start gate.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Second click arrives while task1 is stuck — must be dropped.
    task2 = asyncio.create_task(ctrl.arm_from_tray())
    await asyncio.sleep(0)
    assert task2.done()  # returned immediately via the in-flight guard

    gate.set()
    await task1
    assert ctrl.state == ArmState.ARMED
    assert mic.start_count == 1 and sys_cap.start_count == 1

    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_state_change_callback_fires_on_arm_and_disarm():
    """__main__'s _tray_bridge registers a callback so the tray menu
    label updates the moment arm state flips — not 0.5 s later on the
    next poll. Verify the hook fires for both directions."""
    ctrl, _, *_ = _make_controller()
    events: list[ArmState] = []
    ctrl.set_state_change_callback(lambda: events.append(ctrl.state))

    await ctrl.arm_from_tray()
    assert events == [ArmState.ARMED]

    await ctrl.arm_from_tray()
    assert events == [ArmState.ARMED, ArmState.DISARMED]


async def test_rebind_hotkey_surfaces_error_on_conflict():
    ctrl, _, *_ = _make_controller()

    class _FakeHotkey:
        binding = "ctrl+alt+s"

        def rebind(self, new_binding: str):
            return "That shortcut is already in use by another app"

        def unregister(self) -> None:
            pass

    ctrl._hotkey = _FakeHotkey()
    err = ctrl.rebind_hotkey("alt+f4")
    assert err is not None
    assert "already in use" in err
    assert ctrl.cfg.hotkey == "ctrl+alt+s"  # unchanged


# ---- notification gating toggles ---------------------------------------
#
# The four new ArmConfig toggles (checkin_enabled,
# meeting_ended_watcher_enabled, confirm_hotkey_stop,
# notify_session_wrapped) gate user-visible toasts / consents that
# previously always fired. Each test sets the flag false, drives the
# matching code path, and asserts the toast / consent didn't fire.


async def test_long_meeting_checkin_skipped_when_disabled():
    """Disabling ``checkin_enabled`` makes ``_run_checkins`` short-circuit
    with no consent toast even after the configured mark elapses."""
    notifier = FakeNotifier()
    # Mark every 0.02 s so we don't have to wait.
    ctrl, *_, _ = _make_controller(
        notifier=notifier,
        cfg_overrides={
            "checkin_enabled": False,
            "long_meeting_checkin_marks_secs": [0.02],
        },
    )
    task = asyncio.create_task(ctrl._run_checkins(time.monotonic()))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert notifier.consent_calls == []


async def test_long_meeting_checkin_fires_when_enabled():
    """Sanity counterpart: enabling the flag (default) still fires."""
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, *_, _ = _make_controller(
        notifier=notifier,
        cfg_overrides={
            "checkin_enabled": True,
            "long_meeting_checkin_marks_secs": [0.02],
        },
    )
    ctrl.state = ArmState.ARMED  # bypass real arm
    task = asyncio.create_task(ctrl._run_checkins(time.monotonic()))
    await asyncio.sleep(0.25)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(c["title"] == "Still in the meeting?" for c in notifier.consent_calls)


async def test_meeting_ended_watcher_skipped_when_disabled():
    """Disabling ``meeting_ended_watcher_enabled`` makes
    ``_run_meeting_ended_watcher`` short-circuit immediately — no polling,
    no toast."""
    notifier = FakeNotifier()
    ctrl, *_, _ = _make_controller(
        notifier=notifier,
        cfg_overrides={"meeting_ended_watcher_enabled": False},
    )
    ctrl.state = ArmState.ARMED
    reason = ArmReason(source="whitelist", app_key="zoom", display_name="Zoom")
    task = asyncio.create_task(ctrl._run_meeting_ended_watcher(reason))
    await asyncio.sleep(0.1)
    # Should have completed (early return) without firing anything.
    assert task.done()
    assert notifier.consent_calls == []
    assert all("Looks like your meeting ended" not in t for t, _ in notifier.fire_and_forget)


async def test_hotkey_disarm_skips_confirm_when_disabled():
    """With ``confirm_hotkey_stop`` off, pressing the hotkey while armed
    disarms immediately — no consent toast is shown."""
    notifier = FakeNotifier()
    # Only the initial arm consent is consumed; no second consent should fire.
    notifier.consent_script = ["yes"]
    ctrl, *_, _ = _make_controller(
        notifier=notifier,
        cfg_overrides={"confirm_hotkey_stop": False},
    )

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED

    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.DISARMED
    # Only the arm consent fired ("Start recording?"); no "Stop recording?".
    assert all(c["title"] != "Stop recording?" for c in notifier.consent_calls)


async def test_session_wrapped_notify_skipped_when_disabled():
    """With ``notify_session_wrapped`` off, the post-Keep-going force-close
    branch disarms silently — no "Wrapped up your session" toast. Drives
    the real ``_run_meeting_ended_watcher`` so the production gate is
    actually exercised (mirrors ``test_meeting_ended_keep_going_force_closes_after_threshold``)."""
    notifier = FakeNotifier()
    # Whitelist yes → arm. Meeting-ended toast → "Keep going" (no).
    notifier.consent_script = ["yes", "no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={
            "poll_interval_secs": 0.01,
            "whitelist_arm_release_grace_secs": 0.02,
            "force_close_after_keep_going_secs": 0.06,
            "meeting_ended_toast_timeout_secs": 0.02,
            "notify_session_wrapped": False,
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.ARMED
    ctrl._q_mic_holders = lambda: []
    await asyncio.sleep(0.35)
    assert ctrl.state == ArmState.DISARMED
    # Production gate is exercised: force-close hit, watcher branched
    # through the notify_session_wrapped check, and the toast was
    # suppressed.
    info_calls = [t for t in notifier.fire_and_forget if "Wrapped up" in t[0]]
    assert info_calls == [], (
        f"expected no 'Wrapped up' toast when disabled, got {info_calls!r}"
    )

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


# ---- off-loop snapshots (v3.20 QueueFull-storm fix) -------------------


async def test_async_snapshot_runs_off_event_loop():
    """A slow platform snapshot must NOT freeze the event loop.

    Regression guard for the QueueFull storm: running the macOS audio-detect
    subprocess snapshot directly on the loop blocked it ~1.9 s/poll, starving
    the capture-queue consumer. The watchers now await the snapshot via
    run_in_executor; this proves a blocking snapshot lets concurrent loop
    work proceed.
    """
    import time as _time

    ctrl, *_ = _make_controller()
    ctrl._loop = asyncio.get_running_loop()

    def _slow_holders():
        _time.sleep(0.15)  # blocking, like a real audio-detect call
        return []

    ctrl._q_mic_holders = _slow_holders  # type: ignore[assignment]

    counter = 0

    async def _spin():
        nonlocal counter
        while True:
            counter += 1
            await asyncio.sleep(0.005)

    spinner = asyncio.create_task(_spin())
    await ctrl._async_snapshot_mic_state()
    spinner.cancel()
    try:
        await spinner
    except asyncio.CancelledError:
        pass

    # If the snapshot had run on the loop, the spinner would have been frozen
    # for the whole 0.15 s (~0 ticks). Off-loop, it advances many times.
    assert counter > 5
