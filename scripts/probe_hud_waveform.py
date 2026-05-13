"""HUD waveform probe.

Spawns the real HUD subprocess (via `HudLauncher` -- the same code path
the production agent uses), shows the pill, and pushes audio levels via
`set_audio_levels` so you can visually confirm whether the pill waveform
bars actually react.

Four modes:

* `synth` (default) -- pushes a synthetic 0->1->0 sweep at 20 Hz for ~10 s,
  then a few stress patterns (full-scale flicker, silence, etc.). If the
  bars rise and fall in time with the sweep, the wire from
  `set_audio_levels` -> React Waveform is intact. If they stay flat,
  something downstream of the launcher is broken.

* `live-buggy` -- opens the real mic + system streams and pushes the
  `app.py` formula (the broken one). Reproduces what production looks
  like today: bars sit at minimum height regardless of what you say into
  the mic.

* `live-fixed` -- opens the real mic + system streams and pushes raw
  dtype-aware RMS (the first fix). Bars should rise and fall as you
  speak but system audio may still look weak.

* `live-normalized` -- opens the real streams, applies the same
  per-source slow-peak normalization that the agent now uses, and pushes
  normalized levels. Mic and system both fill the bars during their
  respective speech / playback. This is what production sends after
  v2.14.0.

Usage:
    python scripts/probe_hud_waveform.py
    python scripts/probe_hud_waveform.py synth
    python scripts/probe_hud_waveform.py live-buggy
    python scripts/probe_hud_waveform.py live-fixed
    python scripts/probe_hud_waveform.py live-normalized

Ctrl+C exits cleanly. The HUD subprocess is terminated on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
import time

import numpy as np


def _buggy_rms(frame: np.ndarray) -> float:
    """Exact app.py formula. int16-assumption; broken for float32 frames."""
    if not frame.size:
        return 0.0
    try:
        return float(np.sqrt(np.mean(frame * frame, dtype=np.float32))) / 32768.0
    except Exception:
        return 0.0


def _correct_rms(frame: np.ndarray) -> float:
    """Dtype-aware RMS in [0, 1] for both int16 and float32 inputs."""
    if not frame.size:
        return 0.0
    if np.issubdtype(frame.dtype, np.integer):
        f = frame.astype(np.float64)
        return float(np.sqrt(np.mean(f * f))) / 32768.0
    f = frame.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(f * f)))


class _PeakNormalizer:
    """Mirror of Agent._consume's per-source peak normalizer.

    Kept local to the script so it stays runnable even if app.py's
    internals change. Should match the constants in app.py.
    """

    DECAY = 0.995

    def __init__(self, init_peak: float, min_peak: float) -> None:
        self._peak = init_peak
        self._min = min_peak

    def push(self, rms: float) -> float:
        if rms > self._peak:
            self._peak = rms
        else:
            self._peak *= self.DECAY
        denom = max(self._peak, self._min)
        return min(1.0, rms / denom) if denom > 0 else 0.0

    @property
    def peak(self) -> float:
        return self._peak


async def _wait_hud_ready(launcher) -> bool:
    print("waiting for HUD subprocess to become ready (up to 30 s)...")
    ok = await launcher.wait_for_ready(timeout_secs=30.0)
    if not ok:
        print("HUD never emitted hud_ready -- bailing.")
        return False
    print("HUD ready.\n")
    return True


def _show_pill(launcher) -> None:
    launcher.show_pill(
        reason="hotkey",
        reason_label="Hotkey",
        start_ts=time.time(),
        hotkey="Ctrl+Alt+S",
    )


async def _run_synth(launcher) -> None:
    """Drive set_audio_levels with synthetic patterns; print what we send."""
    print("=== synth phase ===")
    print("If the pill waveform reacts to these pushes, the IPC + React path is fine.\n")

    _show_pill(launcher)
    await asyncio.sleep(0.5)

    async def push(label: str, mic: float, sysv: float, hold_secs: float) -> None:
        launcher.set_audio_levels(mic, sysv)
        print(f"  push  {label:<20}  mic={mic:.3f}  system={sysv:.3f}")
        await asyncio.sleep(hold_secs)

    # 1. Hold zero -- bars should sit at minimum height.
    await push("zero", 0.0, 0.0, 1.0)

    # 2. Slow ramp up, then down.
    print("\n  slow ramp up (~3 s)")
    steps = 60
    for i in range(steps + 1):
        level = i / steps
        launcher.set_audio_levels(level, level)
        await asyncio.sleep(3.0 / steps)
    print("  slow ramp down (~3 s)")
    for i in range(steps + 1):
        level = 1.0 - i / steps
        launcher.set_audio_levels(level, level)
        await asyncio.sleep(3.0 / steps)

    # 3. Sine modulation -- clearly time-varying.
    print("\n  sine modulation (~5 s @ 1 Hz)")
    started = time.monotonic()
    while time.monotonic() - started < 5.0:
        t = time.monotonic() - started
        level = 0.5 + 0.5 * math.sin(2 * math.pi * t)
        launcher.set_audio_levels(level, level * 0.8)
        await asyncio.sleep(1.0 / 30.0)

    # 4. Hard transition: silence -> full scale -> silence.
    await push("\nfull-scale spike", 1.0, 1.0, 1.0)
    await push("back to zero", 0.0, 0.0, 1.0)

    # 5. Asymmetric: only mic, then only system.
    print("\n  mic only @ 0.8 (system silent) -- 2 s")
    for _ in range(40):
        launcher.set_audio_levels(0.8, 0.0)
        await asyncio.sleep(0.05)
    print("  system only @ 0.8 (mic silent) -- 2 s")
    for _ in range(40):
        launcher.set_audio_levels(0.0, 0.8)
        await asyncio.sleep(0.05)

    print("\nsynth phase done.")


async def _run_live(launcher, rms_fn, *, normalize: bool) -> None:
    """Open real mic+system, push their level into the HUD until Ctrl+C.

    When ``normalize`` is True, the levels pushed to the HUD are the
    per-source peak-normalized values (same math the agent uses); the
    printed line shows raw RMS, peak, and normalized side-by-side so the
    bug-or-fix can be confirmed at a glance.
    """
    if rms_fn is _buggy_rms:
        label = "BUGGY"
    elif normalize:
        label = "FIXED+NORMALIZED"
    else:
        label = "FIXED"
    print(f"=== live phase ({label}) ===")
    print("Say something into the mic and play audio. Watch the pill waveform.\n")

    _show_pill(launcher)
    await asyncio.sleep(0.5)

    from sayzo_agent.capture.mic import MicCapture
    from sayzo_agent.capture import SystemCapture

    mic = MicCapture()
    sys_cap = SystemCapture(system_scope="endpoint") if SystemCapture is not None else None

    await mic.start()
    if sys_cap is not None:
        await sys_cap.start()
    print("streams open.\n")

    latest = {"mic_rms": 0.0, "sys_rms": 0.0,
              "mic_send": 0.0, "sys_send": 0.0,
              "mic_peak": 0.0, "sys_peak": 0.0}

    mic_norm = _PeakNormalizer(init_peak=0.05, min_peak=0.015) if normalize else None
    sys_norm = _PeakNormalizer(init_peak=0.02, min_peak=0.004) if normalize else None

    async def drain(name: str, q):
        while True:
            try:
                _, frame = await q.get()
            except asyncio.CancelledError:
                break
            rms = rms_fn(frame)
            latest[f"{name}_rms"] = rms
            if name == "mic":
                if mic_norm is not None:
                    latest["mic_send"] = mic_norm.push(rms)
                    latest["mic_peak"] = mic_norm.peak
                else:
                    latest["mic_send"] = rms
            else:
                if sys_norm is not None:
                    latest["sys_send"] = sys_norm.push(rms)
                    latest["sys_peak"] = sys_norm.peak
                else:
                    latest["sys_send"] = rms

    tasks = [asyncio.create_task(drain("mic", mic.queue))]
    if sys_cap is not None:
        tasks.append(asyncio.create_task(drain("sys", sys_cap.queue)))

    last_print = 0.0
    try:
        while True:
            launcher.set_audio_levels(latest["mic_send"], latest["sys_send"])
            now = time.monotonic()
            if now - last_print >= 0.5:
                if normalize:
                    print(
                        f"  mic rms={latest['mic_rms']:.4f} peak={latest['mic_peak']:.4f} "
                        f"-> {latest['mic_send']:.3f}    "
                        f"sys rms={latest['sys_rms']:.4f} peak={latest['sys_peak']:.4f} "
                        f"-> {latest['sys_send']:.3f}"
                    )
                else:
                    print(f"  mic={latest['mic_send']:.6f}   system={latest['sys_send']:.6f}   "
                          f"(combined={max(latest['mic_send'], latest['sys_send']):.6f})")
                last_print = now
            await asyncio.sleep(1.0 / 20.0)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await mic.stop()
        if sys_cap is not None:
            await sys_cap.stop()


async def _main(args: argparse.Namespace) -> int:
    from sayzo_agent.gui.hud.launcher import HudLauncher

    launcher = HudLauncher()
    await launcher.start()
    try:
        if not await _wait_hud_ready(launcher):
            return 1
        if args.mode == "synth":
            await _run_synth(launcher)
        elif args.mode == "live-buggy":
            await _run_live(launcher, _buggy_rms, normalize=False)
        elif args.mode == "live-fixed":
            await _run_live(launcher, _correct_rms, normalize=False)
        elif args.mode == "live-normalized":
            await _run_live(launcher, _correct_rms, normalize=True)
        # Hold the HUD on screen briefly after synth so the final state
        # is visible before we tear down.
        if args.mode == "synth":
            print("\nholding HUD on screen for 3 s before quit (look at the pill)...")
            await asyncio.sleep(3.0)
    finally:
        launcher.hide_pill()
        await asyncio.sleep(0.2)
        await launcher.quit()
        print("HUD subprocess terminated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "mode",
        nargs="?",
        default="synth",
        choices=("synth", "live-buggy", "live-fixed", "live-normalized"),
        help="which probe to run (default: synth)",
    )
    parser.add_argument("--verbose", action="store_true", help="enable INFO logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
