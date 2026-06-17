"""Probe: VAD-emitted monotonic timestamps survive the round trip to
session-relative segments through the production ArmController path.

Background -- the v2.18 refactor
-------------------------------
v2.18 collapsed the duplicate clock-state machinery in
``conversation.py`` by making ``SileroVAD.feed(frame, frame_mono_ts)``
take a per-frame monotonic timestamp and emit ``SpeechSegment`` with
monotonic ``start_ts`` / ``end_ts``. The detector's ``on_segment`` then
subtracts ``_session_t0_mono`` to get session-relative seconds.

The seam this probe exercises:

  controller arms -> VAD reset -> detector.open_session_on_arm anchors session_t0
  -> frames flow -> VAD.feed receives (frame, capture_mono_ts)
  -> VAD emits SpeechSegment(mono_start, mono_end)
  -> detector.on_segment rebases to (mono - session_t0)
  -> session closes -> mic_segments contains session-relative timestamps

This is the PRODUCTION path (ArmController + ConversationDetector).
Critically NOT the ``replay`` subcommand path -- that has its own teardown
wiring that masked the v2.17 flush-on-close bug.

Usage
-----
::

    python scripts/probe_vad_timestamps.py

What you should see
-------------------
PASS:
  All three scenarios report rebased session-relative times that match
  expectations within frame-duration tolerance.

FAIL:
  The seam between VAD-emits-monotonic and detector-rebases is broken.
  Either VAD isn't using ``frame_mono_ts`` correctly (check
  ``vad.py::_buf_start_mono`` tracking) or detector's ``on_segment``
  isn't subtracting ``_session_t0_mono`` (check ``conversation.py``).
"""
from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    from sayzo_agent.arm.controller import ArmController, ArmState
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

    def build_controller():
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

    print("Probe: VAD monotonic timestamps round-trip through ArmController")
    print("----------------------------------------------------------------")

    failures = 0

    # ---- Scenario 1: single arm, mic segment with mono timestamps ----
    print("\n[1] single arm -- mic segment with mono times rebases correctly")
    ctrl, detector, notifier = build_controller()
    notifier.script = ["yes", "yes"]
    await ctrl._on_hotkey_pressed()
    assert ctrl.state == ArmState.ARMED, f"arm failed (state={ctrl.state})"
    session_t0 = detector._session_t0_mono
    print(f"    session_t0_mono = {session_t0:.6f}")

    # Set up pending segment as if VAD ran during the session -- mono times
    # are session_t0 + offsets.
    expected_starts = [1.0, 7.3]
    expected_ends = [3.5, 9.8]
    ctrl.vad_mic.pending = [
        SpeechSegment("mic", session_t0 + expected_starts[0], session_t0 + expected_ends[0]),
        SpeechSegment("mic", session_t0 + expected_starts[1], session_t0 + expected_ends[1]),
    ]

    await ctrl._on_hotkey_pressed()  # disarm
    assert ctrl.state == ArmState.DISARMED, "disarm failed"

    closed = detector.take_closed_session()
    assert closed is not None, "no closed session"
    if len(closed.mic_segments) != 2:
        print(f"  [FAIL] expected 2 mic_segments, got {len(closed.mic_segments)}")
        failures += 1
    else:
        ok = True
        for i, seg in enumerate(closed.mic_segments):
            if abs(seg.start_ts - expected_starts[i]) > 0.001:
                print(f"  [FAIL] seg{i}.start_ts={seg.start_ts:.6f} expected {expected_starts[i]}")
                ok = False
            if abs(seg.end_ts - expected_ends[i]) > 0.001:
                print(f"  [FAIL] seg{i}.end_ts={seg.end_ts:.6f} expected {expected_ends[i]}")
                ok = False
        if ok:
            print(f"  [PASS] both segments rebased correctly: {[(s.start_ts, s.end_ts) for s in closed.mic_segments]}")
        else:
            failures += 1

    # ---- Scenario 2: two consecutive arms, second arm's segments anchored to second session_t0 ----
    print("\n[2] arm/disarm/arm -- second arm's segments anchor to NEW session_t0")
    ctrl, detector, notifier = build_controller()
    notifier.script = ["yes", "yes", "yes", "yes"]
    await ctrl._on_hotkey_pressed()
    session_t0_a = detector._session_t0_mono
    ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0_a + 2.0, session_t0_a + 4.5)]
    await ctrl._on_hotkey_pressed()
    closed_a = detector.take_closed_session()

    # Wait a tiny bit so session_t0_b is observably different.
    await asyncio.sleep(0.05)

    await ctrl._on_hotkey_pressed()  # second arm
    session_t0_b = detector._session_t0_mono
    assert session_t0_b > session_t0_a, "session_t0 didn't advance"
    print(f"    session_t0_a = {session_t0_a:.6f}, session_t0_b = {session_t0_b:.6f}, delta = {(session_t0_b-session_t0_a)*1000:.1f} ms")

    ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0_b + 1.5, session_t0_b + 3.0)]
    await ctrl._on_hotkey_pressed()
    closed_b = detector.take_closed_session()

    a_ok = (
        closed_a is not None
        and len(closed_a.mic_segments) == 1
        and abs(closed_a.mic_segments[0].start_ts - 2.0) < 0.001
        and abs(closed_a.mic_segments[0].end_ts - 4.5) < 0.001
    )
    b_ok = (
        closed_b is not None
        and len(closed_b.mic_segments) == 1
        and abs(closed_b.mic_segments[0].start_ts - 1.5) < 0.001
        and abs(closed_b.mic_segments[0].end_ts - 3.0) < 0.001
    )

    if a_ok and b_ok:
        print("  [PASS] both arms produced independently anchored segments")
    else:
        if not a_ok:
            print(f"  [FAIL] arm A segment off: {closed_a.mic_segments if closed_a else 'no session'}")
        if not b_ok:
            print(f"  [FAIL] arm B segment off: {closed_b.mic_segments if closed_b else 'no session'}")
        failures += 1

    # ---- Scenario 3: cross-source (mic + system) segments rebased independently ----
    print("\n[3] mic + system segments rebase to same session_t0 without cross-source drift")
    ctrl, detector, notifier = build_controller()
    notifier.script = ["yes", "yes"]
    await ctrl._on_hotkey_pressed()
    session_t0 = detector._session_t0_mono

    ctrl.vad_mic.pending = [SpeechSegment("mic", session_t0 + 0.5, session_t0 + 2.0)]
    ctrl.vad_sys.pending = [SpeechSegment("system", session_t0 + 1.0, session_t0 + 3.5)]

    await ctrl._on_hotkey_pressed()
    closed = detector.take_closed_session()
    mic_ok = (
        closed is not None
        and len(closed.mic_segments) == 1
        and abs(closed.mic_segments[0].start_ts - 0.5) < 0.001
        and abs(closed.mic_segments[0].end_ts - 2.0) < 0.001
    )
    sys_ok = (
        closed is not None
        and len(closed.sys_segments) == 1
        and abs(closed.sys_segments[0].start_ts - 1.0) < 0.001
        and abs(closed.sys_segments[0].end_ts - 3.5) < 0.001
    )
    if mic_ok and sys_ok:
        print(f"  [PASS] mic ({closed.mic_segments[0].start_ts:.2f}->{closed.mic_segments[0].end_ts:.2f}) + "
              f"system ({closed.sys_segments[0].start_ts:.2f}->{closed.sys_segments[0].end_ts:.2f})")
    else:
        if closed is None:
            print("  [FAIL] no closed session")
        else:
            if not mic_ok:
                print(f"  [FAIL] mic segments off: {closed.mic_segments}")
            if not sys_ok:
                print(f"  [FAIL] sys segments off: {closed.sys_segments}")
        failures += 1

    print("\nResult")
    print("------")
    if failures == 0:
        print("PASS: VAD monotonic timestamps survive the full ArmController seam.")
        return 0
    print(f"FAIL: {failures} scenario(s) regressed.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
