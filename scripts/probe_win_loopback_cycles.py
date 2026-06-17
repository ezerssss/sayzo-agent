"""Live probe for the Windows process-loopback "0.0s on second capture" bug.

Reproduces the disarm/re-arm path that the user reported on Windows, where
the SECOND capture against the same target PID produces no system audio.

What this exercises
-------------------
``ProcessLoopbackCapture`` from ``sayzo_agent.capture.system_win_process`` is
the exact class the agent uses on Windows when the arm reason carries
target PIDs (whitelist app match or hotkey smart-guess). Each cycle:

    * ProcessLoopbackCapture(target_pids).start()
        → activates / wakes the persistent thread for each PID
        → audio_client.Start() inside the thread
    * drain frames from .queue for `--cycle-secs` seconds
    * .stop()
        → deactivates the persistent thread
        → audio_client.Stop() inside the thread

Between cycles, the persistent thread parks on `_wake.wait()` with the
audio client in the Stopped state. The agent's actual lifecycle does the
same thing — Stop on disarm, Start on re-arm — and that's where the
"second capture is silent" symptom appears in the field.

Usage
-----
1. Open Spotify / YouTube / a Discord call — anything that's actively
   producing speaker output. Mute notification sounds so the audio
   under test is steady.

2. Find the target process's PID. Easy way::

       Get-Process spotify | Select-Object -First 1 -ExpandProperty Id

   For Discord/Teams you'll get many helper PIDs; pass any one — the
   `INCLUDE_TARGET_PROCESS_TREE` activation mode picks up the rest.

3. Run::

       python scripts/probe_win_loopback_cycles.py --pid <PID>

   Optional flags::

       --cycle-secs 8   # how long to drain audio per cycle (default 5)
       --cycles 3       # how many activate/deactivate cycles (default 2)
       --pause-secs 2   # idle gap between cycles (default 2)
       --save-wav       # dump per-cycle audio so you can A/B listen

What you should see
-------------------
PASS: every cycle reports frames received and a non-zero |sample| sum.
      A small deviation between cycles is fine — they're independent
      reads of a live stream.

FAIL: cycle 1 has frames + non-zero sum, cycle 2 (and later) has 0
      frames OR all-zero samples. That's the regression — confirms
      that ``audio_client.Stop()`` followed by ``Start()`` doesn't
      reliably resume process-loopback delivery without ``Reset()``,
      and we should restructure to Microsoft's never-Stop pattern.

Per-cycle log lines from system_win_process are forwarded with a
[proc-loopback] prefix; you'll see "activated client for pid=..."
exactly ONCE (first cycle), then "paused pid=..." on each disarm.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import wave
from pathlib import Path

import numpy as np


# Force UTF-8 stdout so the printout doesn't crash on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _setup_logging() -> None:
    """Verbose logging for both the probe and the proc-loopback module."""
    fmt = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-40s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Bump the proc-loopback module to DEBUG so the inner Start/Stop +
    # buffer-drain breadcrumbs show up.
    logging.getLogger("sayzo_agent.capture.system_win_process").setLevel(logging.DEBUG)


async def _drain_cycle(
    capture,
    label: str,
    cycle_secs: float,
    save_wav: Path | None,
) -> tuple[int, float, float]:
    """Activate, drain `cycle_secs` seconds of frames, deactivate.

    Returns ``(frame_count, abs_sum, peak)``. ``abs_sum`` is sum of
    |sample| across all frames — a strict "did we get audio?" signal.
    ``peak`` is max |sample| — useful for distinguishing "queue full of
    zeros" from "queue full of music".
    """
    print(f"\n=== {label}: starting capture ===")
    cycle_t0_wall = time.monotonic()
    await capture.start()
    print(f"  start() returned in {time.monotonic() - cycle_t0_wall:.2f}s")

    deadline = time.monotonic() + cycle_secs
    frame_count = 0
    abs_sum = 0.0
    peak = 0.0
    accum: list[np.ndarray] = []

    while time.monotonic() < deadline:
        try:
            ts, frame = await asyncio.wait_for(capture.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        frame_count += 1
        if frame.size > 0:
            abs_sum += float(np.sum(np.abs(frame)))
            peak = max(peak, float(np.max(np.abs(frame))))
        if save_wav is not None:
            accum.append(frame)

    await capture.stop()
    print(f"  stop() returned at +{time.monotonic() - cycle_t0_wall:.2f}s")

    if save_wav is not None and accum:
        pcm = np.concatenate(accum)
        pcm_i16 = np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)
        with wave.open(str(save_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(capture.sample_rate)
            w.writeframes(pcm_i16.tobytes())
        print(f"  wav: {save_wav.resolve()} ({len(pcm) / capture.sample_rate:.2f}s)")

    print(
        f"  frames={frame_count} |abs_sum|={abs_sum:.2f} peak={peak:.4f} "
        f"avg_per_frame={abs_sum / max(1, frame_count):.4f}"
    )
    return frame_count, abs_sum, peak


async def _async_main(
    pid: int,
    cycles: int,
    cycle_secs: float,
    pause_secs: float,
    save_wav: bool,
) -> int:
    # Lazy imports so the script can show its own argparse help on non-Windows
    # platforms without crashing.
    if sys.platform != "win32":
        print("ERROR: this probe only works on Windows (WASAPI loopback).")
        return 2

    from sayzo_agent.capture.system_win_process import (
        ProcessLoopbackCapture,
        is_supported,
    )

    if not is_supported():
        print(
            "ERROR: this Windows build is too old for WASAPI process loopback "
            "(need Windows 10 build 19041 / version 2004 / May 2020 or newer)."
        )
        return 2

    print(f"target pid: {pid}")
    print(f"cycles: {cycles}, cycle_secs: {cycle_secs}, pause_secs: {pause_secs}")

    out_dir = Path("./probe_win_loopback_out")
    if save_wav:
        out_dir.mkdir(exist_ok=True)

    # Critical: re-use ONE ProcessLoopbackCapture instance for the wrapper
    # is fine — but the persistent thread under it (``_PERSISTENT_THREADS``)
    # IS the part that's reused by the real agent across arm cycles. To
    # exercise the real-world path we should create a NEW
    # ProcessLoopbackCapture per cycle, since that's what
    # SystemCapture._try_start_process_loopback does after each disarm
    # (see system_win.SystemCapture.start: `delegate = ...; self._process_loopback = delegate`,
    # and stop: `self._process_loopback = None`).
    results: list[tuple[int, float, float]] = []
    for i in range(1, cycles + 1):
        capture = ProcessLoopbackCapture(
            (pid,), sample_rate=16_000, frame_ms=20,
        )
        wav_path = (out_dir / f"cycle_{i}.wav") if save_wav else None
        try:
            stats = await _drain_cycle(
                capture, f"cycle {i}/{cycles}", cycle_secs, wav_path,
            )
        except Exception as exc:
            print(f"  CYCLE {i} CRASHED: {exc!r}")
            return 1
        results.append(stats)
        if i < cycles:
            print(f"\n  ...idle gap {pause_secs}s (mimics disarm interval)...")
            await asyncio.sleep(pause_secs)

    print("\n=== summary ===")
    for i, (frames, abs_sum, peak) in enumerate(results, 1):
        verdict = "AUDIO" if abs_sum > 1.0 else "SILENT"
        print(
            f"  cycle {i}: frames={frames:5d}  "
            f"|abs_sum|={abs_sum:10.2f}  peak={peak:.4f}  -> {verdict}"
        )

    # Verdict: cycle 1 had audio AND any later cycle is silent → bug confirmed.
    first = results[0]
    later_silent = [
        i for i, (frames, abs_sum, _peak) in enumerate(results[1:], 2)
        if abs_sum <= 1.0 or frames == 0
    ]
    if first[1] > 1.0 and later_silent:
        print(
            f"\nFAIL: cycle 1 captured audio (|sum|={first[1]:.2f}) but "
            f"cycle(s) {later_silent} were silent. Confirms the Stop/Start "
            f"regression — fix path is to add audio_client.Reset() between "
            f"cycles, or to never Stop the audio client (Microsoft's pattern)."
        )
        return 1
    if first[1] <= 1.0:
        print(
            "\nINCONCLUSIVE: cycle 1 itself had no audio. Make sure the "
            "target PID is actually playing through the speakers (not muted, "
            "not in the wrong audio device). Try a different PID."
        )
        return 2
    print(
        f"\nPASS: all {len(results)} cycles captured audio. The bug didn't "
        "reproduce on this run — try increasing --pause-secs or using a "
        "different target PID, or run while the system_scope=arm_app whitelist "
        "match is more representative."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--pid", type=int, required=True,
        help="Target process ID — any process actively producing speaker audio. "
             "INCLUDE_TARGET_PROCESS_TREE picks up its descendants automatically.",
    )
    parser.add_argument(
        "--cycles", type=int, default=2,
        help="Number of activate/drain/deactivate cycles (default: 2).",
    )
    parser.add_argument(
        "--cycle-secs", type=float, default=5.0,
        help="Seconds to drain audio per cycle (default: 5.0).",
    )
    parser.add_argument(
        "--pause-secs", type=float, default=2.0,
        help="Idle gap between cycles, mimics disarm-to-rearm interval (default: 2.0).",
    )
    parser.add_argument(
        "--save-wav", action="store_true",
        help="Dump per-cycle audio to ./probe_win_loopback_out/cycle_N.wav so "
             "you can A/B listen for level + duration.",
    )
    args = parser.parse_args()

    _setup_logging()

    return asyncio.run(_async_main(
        pid=args.pid,
        cycles=args.cycles,
        cycle_secs=args.cycle_secs,
        pause_secs=args.pause_secs,
        save_wav=args.save_wav,
    ))


if __name__ == "__main__":
    sys.exit(main())
