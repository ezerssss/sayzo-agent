"""Live probe for the v2.1.5 stale-frame regression.

Reproduces the disarm/arm cycle that caused the 200-second silence bug
in session 2. Captures real microphone audio (via ``MicCapture`` — the
same code the agent uses) through two arm cycles separated by a long
disarm gap, runs the audio through ``ConversationDetector``, and
reports whether the second session's mic_pcm contains any phantom
zero-fill from a stale frame.

Usage:
    python scripts/probe_disarm_arm_cycle.py
    python scripts/probe_disarm_arm_cycle.py --gap-secs 30
    python scripts/probe_disarm_arm_cycle.py --gap-secs 60 --capture-secs 8 --save-wav

Default (60 s gap, 5 s capture each side, no WAV) takes ~70 s. Speak
into the mic during each capture window so the buffers have real
content. The script prints session_t0, mic_pcm length, and a PASS /
FAIL verdict per session — anything off by >2 s of the expected
capture window is the regression returning.

This intentionally runs without STT, the LLM, or the upload path —
nothing else is needed to verify the audio buffer behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
import wave
from pathlib import Path

# Force UTF-8 stdout so the printout doesn't crash on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np

from sayzo_agent.capture.mic import MicCapture
from sayzo_agent.config import ConversationConfig
from sayzo_agent.conversation import ConversationDetector, SessionState
from sayzo_agent.models import SessionCloseReason


SR = 16000


async def _capture_session(
    mic: MicCapture,
    detector: ConversationDetector,
    label: str,
    capture_secs: float,
) -> tuple[float, int, bytes]:
    """Run one arm cycle: open_session_on_arm + capture for N seconds + close.

    Returns ``(mic_pcm_dur_secs, queue_size_at_arm, mic_pcm_bytes)``.
    """
    print(f"\n--- {label}: arming ---")
    arm_now = time.monotonic()
    detector.reset_source_epochs()
    detector.open_session_on_arm(arm_now)
    queue_at_arm = mic.queue.qsize()
    print(f"  mic.queue at arm: {queue_at_arm} stale frame(s)")
    await mic.start()

    print(f"  speak now — capturing for {capture_secs:.1f}s")
    deadline = time.monotonic() + capture_secs
    frames_consumed = 0
    while time.monotonic() < deadline:
        try:
            ts, frame = await asyncio.wait_for(mic.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        detector.on_frame("mic", frame, ts, time.monotonic())
        frames_consumed += 1

    await mic.stop()
    print(f"  consumed {frames_consumed} frames")

    # Close the session and grab its buffers.
    detector.commit_close(time.monotonic(), SessionCloseReason.HOTKEY_END)
    closed = detector.take_closed_session()
    assert closed is not None
    mic_dur = len(closed.mic_pcm) / 2 / SR
    print(f"  session_t0_mono = {closed.session_t0_mono:.3f}")
    print(f"  mic_pcm = {mic_dur:.2f}s ({len(closed.mic_pcm)} bytes)")

    return mic_dur, queue_at_arm, bytes(closed.mic_pcm)


def _save_wav(pcm: bytes, path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm)
    print(f"  wrote {path}")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gap-secs", type=float, default=60.0,
                   help="Disarm gap between session 1 and session 2 (default: 60).")
    p.add_argument("--capture-secs", type=float, default=5.0,
                   help="Length of each capture window (default: 5).")
    p.add_argument("--save-wav", action="store_true",
                   help="Save each session's mic_pcm to a WAV in the cwd.")
    args = p.parse_args()

    cfg = ConversationConfig()  # defaults
    detector = ConversationDetector(cfg, sample_rate=SR)
    mic = MicCapture(sample_rate=SR)

    # --- session 1 ---
    dur1, q1, pcm1 = await _capture_session(
        mic, detector, "session 1", args.capture_secs
    )
    if args.save_wav:
        _save_wav(pcm1, Path("probe_session1.wav"))

    # --- disarm gap ---
    print(f"\n--- disarm gap: sleeping {args.gap_secs:.1f}s ---")
    await asyncio.sleep(args.gap_secs)

    # --- session 2 ---
    dur2, q2, pcm2 = await _capture_session(
        mic, detector, "session 2", args.capture_secs
    )
    if args.save_wav:
        _save_wav(pcm2, Path("probe_session2.wav"))

    # --- verdict ---
    print("\n=== verdict ===")
    expected = args.capture_secs
    tolerance = 2.0  # generous — capture timing isn't sample-accurate
    fail = False

    def report(label: str, dur: float) -> None:
        nonlocal fail
        diff = dur - expected
        ok = abs(diff) <= tolerance
        status = "PASS" if ok else "FAIL"
        print(f"  {label}: mic_pcm={dur:.2f}s expected~{expected:.1f}s "
              f"(off by {diff:+.2f}s) → {status}")
        if not ok:
            fail = True

    report("session 1", dur1)
    report("session 2", dur2)

    if dur2 > expected + 30.0:
        # The specific failure mode: ~200 s of phantom audio prepended.
        print("\n  >>> session 2 has phantom audio prepended. Stale-frame")
        print("  >>> regression is back. Check MicCapture queue drain and")
        print("  >>> ConversationDetector.max_gap_fill_secs.")
        fail = True

    if fail:
        print("\nFAIL")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
