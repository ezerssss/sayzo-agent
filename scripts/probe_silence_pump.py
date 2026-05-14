"""Probe: does the silence pump actually keep WASAPI loopback delivering frames?

Background — the user's 2026-05-14 bug
--------------------------------------
On Windows, WASAPI loopback's documented behavior is to NOT deliver any
packets when nothing is rendering on the endpoint. ``stream.read()`` blocks
forever until something plays. This bit a hotkey-armed session 2 where
the user played 5+ seconds of clear English speech but ``sys_total=0.0s``
came back at session close, gate failed counterparty, session was
discarded.

The v2.17 fix opens a tiny silent render stream on the same WASAPI
endpoint as the loopback — engine clock keeps ticking, loopback gets
continuous packets.

This probe runs the loopback in two modes — pump off, then pump on —
WITH NO AUDIO PLAYING on the endpoint — and counts how many frames each
mode actually delivers. The whole point is to verify that:

  - Pump OFF: ~0 frames in N seconds (proves WASAPI silence-skip is real
    on this machine, not a theoretical concern).
  - Pump ON: continuous frames for the same N seconds (proves the pump
    actually works as a workaround).

Usage
-----
1. **Stop all audio output on this machine.** Mute Spotify, close YouTube,
   pause any video calls. The whole point is to test what happens when
   NOTHING is rendering to the speakers.

2. Run::

       python scripts/probe_silence_pump.py

   Optional flags::

       --secs 15      # how long to drain in each mode (default 10)
       --device "Speakers"   # endpoint name substring to test (default: WASAPI default output)

3. Read the report. PASS criterion is at the bottom.

What you should see
-------------------
PASS:
  Pump OFF: frames_received=0..a-handful, mean_level=0.0
  Pump ON:  frames_received=<lots, ~25/sec batches>, mean_level=0.0 (zeros)
  Conclusion: silence pump is working — loopback delivers continuous
  frames even when nothing is playing.

FAIL (pump OFF gets frames too):
  Either something IS playing audio on this endpoint (re-check muting),
  or your driver/Windows build already delivers silence frames natively
  and the pump is redundant on this machine.

FAIL (pump ON gets 0 frames):
  The pump open failed silently OR your driver rejects the second
  stream. Check ``agent.log``-style stderr for "silence pump open failed"
  and consider running with --no-pump and reporting the driver name.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

if sys.platform != "win32":
    print("ERROR: probe_silence_pump.py is Windows-only (WASAPI).")
    sys.exit(2)


async def _drain(label: str, capture, secs: float) -> dict:
    """Drain frames for `secs` seconds, return summary stats."""
    deadline = time.monotonic() + secs
    frame_count = 0
    sample_count = 0
    abs_sum = 0.0
    first_frame_at: float | None = None
    while time.monotonic() < deadline:
        try:
            ts, frame = await asyncio.wait_for(capture.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if first_frame_at is None:
            first_frame_at = time.monotonic()
        frame_count += 1
        sample_count += int(frame.size)
        import numpy as np
        abs_sum += float(np.abs(frame).sum())
    mean_abs_level = abs_sum / sample_count if sample_count else 0.0
    return {
        "label": label,
        "frame_count": frame_count,
        "sample_count": sample_count,
        "mean_abs_level": mean_abs_level,
        "first_frame_at_delta": (
            (first_frame_at - (deadline - secs)) if first_frame_at else None
        ),
    }


async def _run_one(*, silence_pump_enabled: bool, secs: float) -> dict:
    from sayzo_agent.capture.system_win import SystemCapture
    cap = SystemCapture(
        sample_rate=16000,
        frame_ms=20,
        queue_maxsize=2000,  # generous so we don't lose frames during the test
        silence_pump_enabled=silence_pump_enabled,
    )
    await cap.start()
    # Give it a moment to spin up.
    await asyncio.sleep(0.3)
    summary = await _drain(
        f"pump={'ON' if silence_pump_enabled else 'OFF'}", cap, secs
    )
    await cap.stop()
    return summary


async def _main(args: argparse.Namespace) -> int:
    print("Probe: WASAPI silence-pump effect on loopback delivery")
    print("------------------------------------------------------")
    print(
        "Make sure NO audio is playing on this machine (close Spotify, mute\n"
        "YouTube tabs, pause any calls). Each phase will drain frames for\n"
        f"{args.secs:.0f} s.\n"
    )

    print("Phase 1: silence pump OFF ...")
    off = await _run_one(silence_pump_enabled=False, secs=args.secs)
    print(
        f"  frame_count={off['frame_count']}  "
        f"sample_count={off['sample_count']}  "
        f"mean|sample|={off['mean_abs_level']:.4f}  "
        f"first_frame_delay="
        f"{off['first_frame_at_delta']:.2f}s" if off['first_frame_at_delta'] is not None
        else f"  first_frame_delay=NEVER (no frame in {args.secs:.0f}s)"
    )

    print("\nPhase 2: silence pump ON ...")
    on = await _run_one(silence_pump_enabled=True, secs=args.secs)
    print(
        f"  frame_count={on['frame_count']}  "
        f"sample_count={on['sample_count']}  "
        f"mean|sample|={on['mean_abs_level']:.4f}  "
        + (
            f"first_frame_delay={on['first_frame_at_delta']:.2f}s"
            if on['first_frame_at_delta'] is not None
            else f"first_frame_delay=NEVER (no frame in {args.secs:.0f}s)"
        )
    )

    print("\nResult")
    print("------")
    pump_off_silent = off["frame_count"] < 5
    pump_on_flowing = on["frame_count"] > (args.secs * 30)  # rough: >30 frames/sec
    if pump_off_silent and pump_on_flowing:
        print("PASS: WASAPI silence-skip reproduced, silence pump fixes it.")
        return 0
    if not pump_off_silent and pump_on_flowing:
        print(
            "INCONCLUSIVE: pump OFF still got frames — something is playing\n"
            "audio on this endpoint (re-check muting), or this Windows build\n"
            "already delivers silence natively. Pump still works."
        )
        return 0
    if not pump_on_flowing:
        print(
            "FAIL: pump ON did NOT produce continuous frames. The pump\n"
            "probably failed to open — check stderr for 'silence pump open\n"
            "failed' warnings."
        )
        return 1
    print("UNEXPECTED state; report this output.")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secs", type=float, default=10.0)
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))
