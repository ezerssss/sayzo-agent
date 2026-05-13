"""Live mic + system audio RMS probe.

Opens MicCapture and SystemCapture (the same classes the agent uses
in production), drains each queue at ~10 Hz, and prints two columns:

* `buggy` -- the exact formula currently in
  `sayzo_agent/app.py::Agent._consume`:
  `np.sqrt(np.mean(frame * frame, dtype=np.float32)) / 32768.0`
  This treats the frame as int16 PCM, dividing by full-scale 32768.
* `correct` -- the formula that matches the frame dtype:
  `np.sqrt(np.mean(frame.astype(np.float64) ** 2))`
  Frames are float32 in [-1.0, 1.0] (see `capture/mic.py::dtype="float32"`
  and `capture/system_win.py::np.frombuffer(..., dtype=np.float32)`).

What you should see:

* `correct` reads ~0.02..0.5 during normal speech / music, 0.0..0.01 in
  silence.
* `buggy` reads ~3e-6..1e-5 even when you're shouting -- it never rises
  high enough to cross the 0.005 change-detect threshold in
  `Agent._audio_level_emitter`, so `set_audio_levels` is rarely (or
  never) actually pushed to the HUD. That's why the production pill
  waveform doesn't react.

Usage:
    python scripts/probe_audio_levels.py
    python scripts/probe_audio_levels.py --mic-only
    python scripts/probe_audio_levels.py --system-only
    python scripts/probe_audio_levels.py --duration 30

Ctrl+C exits cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import numpy as np


def _buggy_rms(frame: np.ndarray) -> float:
    """The current app.py formula. Assumes int16; broken for float32."""
    if not frame.size:
        return 0.0
    try:
        return float(np.sqrt(np.mean(frame * frame, dtype=np.float32))) / 32768.0
    except Exception:
        return 0.0


def _correct_rms(frame: np.ndarray) -> float:
    """Dtype-aware RMS. Returns a value in [0, 1] for both int16 and float32 inputs."""
    if not frame.size:
        return 0.0
    if np.issubdtype(frame.dtype, np.integer):
        # int16-style PCM: square in float64 to avoid overflow, normalize by full-scale.
        f = frame.astype(np.float64)
        return float(np.sqrt(np.mean(f * f))) / 32768.0
    # float32 / float64: already in [-1, 1].
    f = frame.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(f * f)))


def _bar(value: float, width: int = 20) -> str:
    """Render a 0..1 value as an ASCII bar. Caps at width."""
    v = max(0.0, min(1.0, value))
    filled = int(round(v * width))
    return "#" * filled + "." * (width - filled)


async def _drain(
    name: str,
    queue: asyncio.Queue,
    state: dict,
) -> None:
    """Pull frames from a capture queue, track latest RMS in ``state``."""
    while True:
        try:
            _, frame = await queue.get()
        except asyncio.CancelledError:
            break
        state[f"{name}_buggy"] = _buggy_rms(frame)
        state[f"{name}_correct"] = _correct_rms(frame)
        state[f"{name}_dtype"] = str(frame.dtype)
        state[f"{name}_samples"] = int(frame.size)
        state[f"{name}_last_ts"] = time.monotonic()


async def _printer(state: dict, run_mic: bool, run_sys: bool, duration: float) -> None:
    """Print one status line at ~10 Hz."""
    print(
        "{:>5}  {:<7}  {:>9}  {:>11}  {:<22}  {:<22}".format(
            "t", "src", "samples", "dtype", "buggy (app.py)", "correct (float32)"
        )
    )
    print("-" * 100)
    started = time.monotonic()
    while True:
        await asyncio.sleep(1.0 / 10.0)
        elapsed = time.monotonic() - started
        if duration > 0 and elapsed >= duration:
            return
        rows = []
        if run_mic:
            rows.append((
                "mic",
                state.get("mic_samples", 0),
                state.get("mic_dtype", "-"),
                state.get("mic_buggy", 0.0),
                state.get("mic_correct", 0.0),
            ))
        if run_sys:
            rows.append((
                "system",
                state.get("system_samples", 0),
                state.get("system_dtype", "-"),
                state.get("system_buggy", 0.0),
                state.get("system_correct", 0.0),
            ))
        for src, samples, dtype, buggy, correct in rows:
            print(
                "{:>5.1f}  {:<7}  {:>9d}  {:>11}  {:.6f} {:<10}  {:.6f} {:<10}".format(
                    elapsed,
                    src,
                    samples,
                    dtype,
                    buggy,
                    _bar(buggy, 6),
                    correct,
                    _bar(correct, 10),
                )
            )
        print()


async def _run(args: argparse.Namespace) -> int:
    from sayzo_agent.capture.mic import MicCapture

    run_mic = not args.system_only
    run_sys = not args.mic_only

    mic = sys_cap = None
    if run_mic:
        mic = MicCapture()
    if run_sys:
        from sayzo_agent.capture import SystemCapture
        if SystemCapture is None:
            print("system capture is not supported on this platform — running mic-only")
            run_sys = False
        else:
            sys_cap = SystemCapture(system_scope="endpoint")

    print("opening capture streams...")
    if mic is not None:
        await mic.start()
    if sys_cap is not None:
        await sys_cap.start()
    print("streams open. Say something / play audio to see the meters move.")
    print("(buggy = the formula in app.py, correct = dtype-aware formula)\n")

    state: dict = {}
    tasks = []
    if mic is not None:
        tasks.append(asyncio.create_task(_drain("mic", mic.queue, state)))
    if sys_cap is not None:
        tasks.append(asyncio.create_task(_drain("system", sys_cap.queue, state)))
    printer = asyncio.create_task(
        _printer(state, run_mic=run_mic and mic is not None,
                 run_sys=run_sys and sys_cap is not None,
                 duration=args.duration)
    )
    try:
        await printer
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if mic is not None:
            await mic.stop()
        if sys_cap is not None:
            await sys_cap.stop()
        print("\nstreams closed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mic-only", action="store_true", help="probe mic only")
    parser.add_argument("--system-only", action="store_true", help="probe system audio only")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--verbose", action="store_true", help="enable INFO logging from agent modules")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
