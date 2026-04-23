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
from typing import Any, Optional

import pytest

from sayzo_agent.arm.controller import ArmController, ArmReason, ArmState
from sayzo_agent.arm.detectors import ForegroundInfo, MicHolder, MicState
from sayzo_agent.config import ArmConfig, ConversationConfig, default_detector_specs
from sayzo_agent.conversation import ConversationDetector
from sayzo_agent.models import SessionCloseReason, SpeechSegment


# ---- fakes ------------------------------------------------------------


class FakeCapture:
    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.fail_next_start = False

    async def start(self) -> None:
        self.start_count += 1
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("simulated capture failure")

    async def stop(self) -> None:
        self.stop_count += 1

    @property
    def is_open(self) -> bool:
        return self.start_count > self.stop_count


class FakeVAD:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1


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
    ) -> str:
        self.consent_calls.append({
            "title": title, "body": body,
            "yes": yes_label, "no": no_label,
            "timeout": timeout_secs,
            "default_on_timeout": default_on_timeout,
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
        "meeting_ended_snooze_secs": 0.05,
        "cooldown_after_decline_secs": 1800.0,
        "cooldown_after_session_secs": 600.0,
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
    vad_m = FakeVAD()
    vad_s = FakeVAD()
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


# ---- whitelist consent -----------------------------------------------


async def test_whitelist_match_fires_consent_and_arms_on_yes():
    notifier = FakeNotifier()
    notifier.consent_script = ["yes"]
    ctrl, _, mic, *_ = _make_controller(
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

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass
    await ctrl._disarm_internal(SessionCloseReason.HOTKEY_END)


async def test_whitelist_decline_sets_cooldown():
    notifier = FakeNotifier()
    notifier.consent_script = ["no"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.15)
    assert ctrl.state == ArmState.DISARMED
    import time
    assert ctrl._cooldowns.active("zoom", time.monotonic()) is True

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


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


async def test_meeting_ended_keep_going_snoozes_and_refires():
    notifier = FakeNotifier()
    # Sequence: whitelist yes, first meeting-ended Keep going, second meeting-ended yes (Wrap up)
    notifier.consent_script = ["yes", "no", "yes"]
    ctrl, _, *_ = _make_controller(
        notifier=notifier,
        mic_holders=[MicHolder("zoom.exe", 1234)],
        cfg_overrides={
            "poll_interval_secs": 0.01,
            "whitelist_arm_release_grace_secs": 0.02,
            "meeting_ended_snooze_secs": 0.06,
            "meeting_ended_toast_timeout_secs": 0.02,
        },
    )
    ctrl._loop = asyncio.get_running_loop()
    ctrl._whitelist_task = asyncio.create_task(ctrl._run_whitelist_watcher())
    await asyncio.sleep(0.05)
    assert ctrl.state == ArmState.ARMED
    ctrl._q_mic_holders = lambda: []
    # Wait for first toast + snooze + second toast + disarm.
    await asyncio.sleep(0.35)
    assert ctrl.state == ArmState.DISARMED
    # Two meeting-ended toasts fired.
    meeting_ended_calls = [c for c in notifier.consent_calls
                           if c["title"] == "Looks like your meeting ended"]
    assert len(meeting_ended_calls) >= 2

    ctrl._whitelist_task.cancel()
    try:
        await ctrl._whitelist_task
    except asyncio.CancelledError:
        pass


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
