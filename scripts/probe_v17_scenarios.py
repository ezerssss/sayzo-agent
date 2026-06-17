"""Probe: end-to-end coverage of the v2.17 bug-class scenarios.

Background — the v2.17 bug session (2026-05-14)
-----------------------------------------------
The user reported a session with three armed cycles on Windows where
session 2's ``sys_total=0.0s`` even though they played clear English
speech. Root causes were:

  1. WASAPI loopback silence-skip: the loopback stream parks on
     ``stream.read()`` when no app is rendering to the endpoint
     (covered separately by ``probe_silence_pump.py``, Windows only).

  2. ``vad.flush()`` was never called on production session close
     (only in the replay subcommand). The in-progress VAD segment
     at hotkey-stop was discarded by the next arm's ``vad.reset()``.

  3. (Latent until #1 was fixed) System capture queue was never drained
     on start/stop, so frames from the previous arm could pollute the
     next session's buffer.

This probe drives the SAME ArmController + ConversationDetector that
production ships, with fake captures + scriptable fake VADs, and walks
through the exact patterns the user's session hit. It also covers the
v2.18 timestamp seam: that two consecutive arm cycles produce
independently-anchored segments (no cross-arm pollution).

The probe deliberately does NOT use the ``replay`` subcommand — in v2.17
that command had its own ``vad.flush()`` call that masked the production
bug. All scenarios go through ``ArmController._on_hotkey_pressed`` so
any future regression of the production wiring fails the probe.

Usage
-----
::

    python scripts/probe_v17_scenarios.py

What you should see
-------------------
6 PASS lines, then ``Result: PASS``. Any FAIL pinpoints which scenario
regressed.
"""
from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    from sayzo_agent.arm.controller import ArmController
    from sayzo_agent.config import ArmConfig, ConversationConfig, default_detector_specs
    from sayzo_agent.conversation import ConversationDetector
    from sayzo_agent.models import SpeechSegment

    class FakeCapture:
        async def start(self, **kwargs):
            pass

        async def stop(self):
            pass

    class FakeVAD:
        def __init__(self, source: str) -> None:
            self.source = source
            self.flush_count = 0
            self.reset_count = 0
            self.pending: list[SpeechSegment] = []

        def reset(self) -> None:
            self.reset_count += 1
            self.pending = []

        def flush(self):
            self.flush_count += 1
            pending = self.pending
            self.pending = []
            for seg in pending:
                yield seg

    class FakeNotifier:
        def __init__(self) -> None:
            self.script: list[str] = []

        def notify(self, title, body) -> None:
            pass

        def ask_consent(self, title, body, yes_label, no_label, timeout, default_on_timeout, supersede=False):
            return self.script.pop(0) if self.script else default_on_timeout

        def __getattr__(self, name):
            return None

    def build():
        cfg = ArmConfig(detectors=default_detector_specs(), hotkey="ctrl+alt+s")
        detector = ConversationDetector(ConversationConfig(joint_silence_close_secs=1.0))
        notifier = FakeNotifier()
        ctrl = ArmController(
            cfg, detector,
            mic_capture=FakeCapture(), sys_capture=FakeCapture(),
            vad_mic=FakeVAD("mic"), vad_sys=FakeVAD("system"),
            notifier=notifier,
            get_mic_holders=lambda: [],
            is_mic_active=lambda: False,
            get_running_processes=lambda: frozenset(),
            system_scope_fn=lambda: "endpoint",
        )
        return ctrl, detector, notifier

    print("Probe: v2.17 bug-class scenarios (drives production ArmController)")
    print("------------------------------------------------------------------")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        marker = "[PASS]" if ok else "[FAIL]"
        print(f"  {marker} {name}" + (f" -- {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # ---- Scenario A: VAD flush recovers mid-utterance segment ----
    # This is the exact v2.17 mic-side bug: user hotkey-stops while still
    # talking, the in-progress segment must NOT be dropped.
    print("\n[A] mic mid-utterance close -- vad.flush() recovers the segment")
    ctrl, detector, notifier = build()
    notifier.script = ["yes", "yes"]
    await ctrl._on_hotkey_pressed()
    session_t0 = detector._session_t0_mono
    ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0 + 0.5, session_t0 + 7.0)]
    await ctrl._on_hotkey_pressed()
    closed = detector.take_closed_session()
    check(
        "vad_mic.flush called exactly once",
        ctrl.vad_mic.flush_count == 1,
        f"flush_count={ctrl.vad_mic.flush_count}",
    )
    check(
        "mic segment recovered into closed session",
        closed is not None and len(closed.mic_segments) == 1,
        f"mic_segments={closed.mic_segments if closed else 'none'}",
    )

    # ---- Scenario B: sys-side flush + session keeps sys_total > 0 ----
    # The v2.17 root cause: system's in-progress segment was the only
    # one (WASAPI silence-skip suppressed everything else), and without
    # flush it never landed in sys_segments -> sys_total = 0.
    print("\n[B] system mid-stream close -- vad_sys.flush() prevents sys_total=0")
    ctrl, detector, notifier = build()
    notifier.script = ["yes", "yes"]
    await ctrl._on_hotkey_pressed()
    session_t0 = detector._session_t0_mono
    # The user's actual playing-system-audio scenario: VAD has one open
    # sys segment when the user hits hotkey. With the v2.17 flush wired,
    # this segment lands in sys_segments and the counterparty gate passes.
    ctrl.vad_sys.pending = [SpeechSegment("system", session_t0 + 1.2, session_t0 + 5.8)]
    # Also a mic segment so the closed session has both sides.
    ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0 + 0.1, session_t0 + 9.0)]
    await ctrl._on_hotkey_pressed()
    closed = detector.take_closed_session()
    check(
        "vad_sys.flush called exactly once",
        ctrl.vad_sys.flush_count == 1,
    )
    check(
        "sys_total > 0 after recovered system segment",
        closed is not None and closed.sys_total_voiced() > 0,
        f"sys_total_voiced={closed.sys_total_voiced() if closed else 'none'}",
    )

    # ---- Scenario C: two consecutive arms, no cross-arm pollution ----
    # The original symptom pattern: arm 1 worked, arm 2 had sys_total=0.
    # Make sure arm 2 produces its own segments anchored to its own t0,
    # without leftover state from arm 1.
    print("\n[C] arm/disarm/arm/disarm -- each session is anchored to its own t0")
    ctrl, detector, notifier = build()
    notifier.script = ["yes", "yes", "yes", "yes"]

    # Arm 1
    await ctrl._on_hotkey_pressed()
    t0_a = detector._session_t0_mono
    ctrl.vad_mic.pending = [SpeechSegment("mic", t0_a + 1.0, t0_a + 4.0)]
    ctrl.vad_sys.pending = [SpeechSegment("system", t0_a + 2.0, t0_a + 3.5)]
    await ctrl._on_hotkey_pressed()
    closed_a = detector.take_closed_session()

    # Tiny gap then arm 2.
    await asyncio.sleep(0.05)
    await ctrl._on_hotkey_pressed()
    t0_b = detector._session_t0_mono
    ctrl.vad_mic.pending = [SpeechSegment("mic", t0_b + 0.5, t0_b + 6.0)]
    ctrl.vad_sys.pending = [SpeechSegment("system", t0_b + 1.0, t0_b + 5.0)]
    await ctrl._on_hotkey_pressed()
    closed_b = detector.take_closed_session()

    a_ok = (
        closed_a is not None
        and len(closed_a.mic_segments) == 1
        and abs(closed_a.mic_segments[0].start_ts - 1.0) < 0.001
        and abs(closed_a.mic_segments[0].end_ts - 4.0) < 0.001
        and closed_a.sys_total_voiced() > 0
    )
    b_ok = (
        closed_b is not None
        and len(closed_b.mic_segments) == 1
        and abs(closed_b.mic_segments[0].start_ts - 0.5) < 0.001
        and abs(closed_b.mic_segments[0].end_ts - 6.0) < 0.001
        and closed_b.sys_total_voiced() > 0
    )
    check(
        "arm 1 session has independent segments + sys_total > 0",
        a_ok,
        f"mic={closed_a.mic_segments if closed_a else 'none'} sys_total={closed_a.sys_total_voiced() if closed_a else 'none'}",
    )
    check(
        "arm 2 session has independent segments + sys_total > 0",
        b_ok,
        f"mic={closed_b.mic_segments if closed_b else 'none'} sys_total={closed_b.sys_total_voiced() if closed_b else 'none'}",
    )

    # ---- Scenario D: every non-hotkey close path also flushes VAD ----
    # The flush helper sits inside _disarm_internal so it fires for any
    # SessionCloseReason: HOTKEY_END / CHECKIN_WRAP_UP / WHITELIST_ENDED /
    # SHUTDOWN all share the same path. Plus _handle_pending_close
    # (JOINT_SILENCE) also calls it. This scenario directly invokes
    # _disarm_internal with each non-hotkey reason to prove the wiring
    # isn't accidentally hotkey-specific.
    print("\n[D] non-hotkey close paths also flush VAD (whitelist auto-arm coverage)")
    from sayzo_agent.models import SessionCloseReason
    non_hotkey_reasons = [
        SessionCloseReason.CHECKIN_WRAP_UP,
        SessionCloseReason.WHITELIST_ENDED,
        SessionCloseReason.SHUTDOWN,
    ]
    for reason in non_hotkey_reasons:
        ctrl, detector, notifier = build()
        notifier.script = ["yes"]  # arm yes; disarm bypasses the toast
        await ctrl._on_hotkey_pressed()
        session_t0 = detector._session_t0_mono
        ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0 + 0.5, session_t0 + 3.0)]
        ctrl.vad_sys.pending = [SpeechSegment("system", session_t0 + 1.0, session_t0 + 2.5)]
        # Directly call _disarm_internal with the reason — this is the path
        # the whitelist watcher / check-in task / shutdown handler hit.
        await ctrl._disarm_internal(reason)
        closed = detector.take_closed_session()
        ok = (
            ctrl.vad_mic.flush_count == 1
            and ctrl.vad_sys.flush_count == 1
            and closed is not None
            and len(closed.mic_segments) == 1
            and len(closed.sys_segments) == 1
            and closed.sys_total_voiced() > 0
        )
        check(
            f"reason={reason.value}: both VADs flushed + segments recovered",
            ok,
            f"mic_flush={ctrl.vad_mic.flush_count} sys_flush={ctrl.vad_sys.flush_count} "
            f"sys_total={closed.sys_total_voiced() if closed else 'none'}",
        )

    print("\nResult")
    print("------")
    if not failures:
        print("PASS: all v2.17 bug-class scenarios held.")
        return 0
    print(f"FAIL: {len(failures)} scenario(s) regressed: {failures}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
