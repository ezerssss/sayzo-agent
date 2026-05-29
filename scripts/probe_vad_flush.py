"""Probe: does ArmController flush in-progress VAD segments on session close?

Background — the user's 2026-05-14 bug
--------------------------------------
SileroVAD only emits a closed ``SpeechSegment`` after ``hangover_ms``
(default 300 ms) of unvoiced chunks. Armed-only sessions close ABRUPTLY
on hotkey_end / check-in wrap-up / meeting-ended / joint-silence-
confirmed — none of those give VAD the trailing silence it needs to
hangover-close its open segment.

Before v2.17.0, the next arm's ``vad.reset()`` simply discarded the
open segment. The result: a user talking continuously through hotkey-
stop loses their LAST (often longest) turn. In the 2026-05-14 logs the
user had 15+ s of continuous speech but max_turn=7.5s came back.

v2.17.0 adds ``ArmController._flush_vads_into_detector`` called BEFORE
``detector.commit_close`` so the open segment lands in the session's
segment list.

This probe drives ArmController with fakes — no real audio — and
verifies that pending VAD segments set up BEFORE hotkey-stop end up
in the closed session.

Usage
-----
::

    python scripts/probe_vad_flush.py

What you should see
-------------------
PASS:
  mic flush: 1 segment recovered
  system flush: 1 segment recovered
  → ArmController.commit_close is wired with the flush hook.

FAIL:
  Either flush_count == 0 (helper never ran) or segment lists are empty
  (helper ran but didn't route through detector.on_segment). Either way,
  open the diff against ``sayzo_agent/arm/controller.py``: the calls
  to ``self._flush_vads_into_detector(now)`` must appear BEFORE every
  ``self.detector.commit_close(now, reason)``.
"""
from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    # Local imports so the probe doesn't pay torch/silero import on boot.
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
            self.reset_count = 0
            self.flush_count = 0
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
            self.script: list[str] = ["yes", "yes"]  # arm yes, disarm yes

        def notify(self, title: str, body: str) -> None:
            pass

        def ask_consent(self, title, body, yes_label, no_label, timeout, default_on_timeout, supersede=False):
            return self.script.pop(0) if self.script else default_on_timeout

        def __getattr__(self, name):
            # Best-effort: notifier accessors used by HUD bookkeeping return None.
            return None

    cfg = ArmConfig(detectors=default_detector_specs(), hotkey="ctrl+alt+s")
    detector = ConversationDetector(ConversationConfig(joint_silence_close_secs=1.0))
    mic = FakeCapture()
    sys_cap = FakeCapture()
    vad_m = FakeVAD("mic")
    vad_s = FakeVAD("system")
    notifier = FakeNotifier()

    ctrl = ArmController(
        cfg, detector,
        mic_capture=mic, sys_capture=sys_cap,
        vad_mic=vad_m, vad_sys=vad_s,
        notifier=notifier,
        get_mic_holders=lambda: [],
        is_mic_active=lambda: False,
        get_running_processes=lambda: frozenset(),
        system_scope_fn=lambda: "endpoint",
    )

    print("Probe: ArmController flushes VAD segments on commit_close")
    print("---------------------------------------------------------")

    await ctrl._on_hotkey_pressed()  # arm with "yes"
    if ctrl.state != ArmState.ARMED:
        print(f"FAIL: arm did not complete (state={ctrl.state})")
        return 1
    print("[ok] armed via hotkey")

    # Simulate VAD holding an open segment when the user hotkey-stops mid-talk.
    # Anchor mono timestamps to the actual session_t0 so the detector's
    # rebase produces predictable session-relative values (v2.18 contract).
    session_t0 = detector._session_t0_mono
    vad_m.pending = [SpeechSegment("mic", session_t0 + 0.5, session_t0 + 6.5)]
    vad_s.pending = [SpeechSegment("system", session_t0 + 1.0, session_t0 + 4.2)]
    expected_mic = (0.5, 6.5)
    expected_sys = (1.0, 4.2)
    print(f"[ok] pre-loaded pending segments (mic: {expected_mic}s, system: {expected_sys}s)")

    await ctrl._on_hotkey_pressed()  # disarm with "yes"
    if ctrl.state != ArmState.DISARMED:
        print(f"FAIL: disarm did not complete (state={ctrl.state})")
        return 1
    print("[ok] disarmed via hotkey")

    closed = detector.take_closed_session()
    if closed is None:
        print("FAIL: no closed session in detector queue")
        return 1
    print(
        f"[ok] closed session retrieved "
        f"(mic_segments={len(closed.mic_segments)}, "
        f"sys_segments={len(closed.sys_segments)})"
    )

    def _within(actual: float, expected: float, tol: float = 0.001) -> bool:
        return abs(actual - expected) < tol

    mic_ok = (
        vad_m.flush_count == 1
        and len(closed.mic_segments) == 1
        and _within(closed.mic_segments[0].start_ts, expected_mic[0])
        and _within(closed.mic_segments[0].end_ts, expected_mic[1])
    )
    sys_ok = (
        vad_s.flush_count == 1
        and len(closed.sys_segments) == 1
        and _within(closed.sys_segments[0].start_ts, expected_sys[0])
        and _within(closed.sys_segments[0].end_ts, expected_sys[1])
    )

    print("\nResult")
    print("------")
    if mic_ok and sys_ok:
        print("PASS: both VADs flushed, both pending segments recovered.")
        return 0
    if vad_m.flush_count == 0 or vad_s.flush_count == 0:
        print(
            f"FAIL: flush helper did not run on both VADs.\n"
            f"  vad_mic.flush_count={vad_m.flush_count}\n"
            f"  vad_sys.flush_count={vad_s.flush_count}\n"
            f"Check ArmController._flush_vads_into_detector is wired into\n"
            f"_disarm_internal AND _handle_pending_close paths."
        )
        return 1
    print(
        f"FAIL: flush ran but segments not in closed session.\n"
        f"  closed.mic_segments={len(closed.mic_segments)} (want 1)\n"
        f"  closed.sys_segments={len(closed.sys_segments)} (want 1)\n"
        f"Check that the flush helper routes through detector.on_segment\n"
        f"BEFORE detector.commit_close — not after."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
