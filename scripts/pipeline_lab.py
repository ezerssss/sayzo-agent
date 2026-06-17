"""Pipeline lab: standalone session recorder + variant producer.

A self-contained script that recreates the live agent's session-close
pipeline so we can A/B individual stages without running the agent itself.

Records mic + system audio using the same capture primitives the live
agent uses (``MicCapture`` + ``SystemCapture``) and aligns the channels
via ``ConversationDetector`` — so mic↔sys timing matches production
exactly, including the v3.6.0 cold-start gap-fill. Then runs the
session-close processing stages incrementally and writes a WAV at each
step. The user listens in order; the first variant where a given
artifact appears identifies the stage that introduced it.

Stages produced (all start from the same captured mic + sys, so the
A/B is honest):

    00_mic_raw.wav            - mic exactly as captured, no processing
    00_sys_raw.wav            - sys exactly as captured
    01_aec.wav                - + WebRTC AEC3 (HPF on, NS off, AGC off)
    02_aec_hpf.wav            - + dsp.py highpass (80 Hz Butter order 4)
    03_aec_hpf_pknorm.wav     - + peak-normalize (-1 dBFS)
    04_aec_full_production.wav - + noisereduce (prop_decrease=0.5)
                                  == current live-agent output
    05_no_aec_full_dsp.wav    - DSP stack WITHOUT AEC (sanity check —
                                shows if static is AEC-dependent)

Usage:
    python scripts/pipeline_lab.py [--duration 30] [--label NAME]

Output: <data_dir>/pipeline-lab/<label or timestamp>/

Prereqs:
    - No other sayzo-agent process running (would compete for the mic).
    - Set up speakers + queue some audio to play during the recording.

Listen in order 00 → 04. The variant where you first hear the artifact
identifies which stage introduced it. Variant 05 is a sanity check —
if static is absent there but present at 04, AEC's interaction with
DSP is part of the picture.
"""
from __future__ import annotations

import argparse
import asyncio
import platform
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

# Force UTF-8 stdout — Windows cp1252 console chokes on the table chars.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sayzo_agent.aec import cancel_echo
from sayzo_agent.capture.mic import MicCapture
from sayzo_agent.config import AecConfig, ConversationConfig
from sayzo_agent.conversation import ConversationDetector
from sayzo_agent.dsp import (
    _apply_highpass,
    _denoise,
    _f32_to_i16,
    _i16_to_f32,
    _peak_normalize,
)
from sayzo_agent.models import SessionCloseReason

if platform.system() == "Windows":
    from sayzo_agent.capture.system_win import SystemCapture
elif platform.system() == "Darwin":
    from sayzo_agent.capture.system_mac import SystemCapture
else:
    raise SystemExit("Pipeline lab supports Windows + macOS only")

SR = 16000

# Production DSP defaults — see CaptureConfig in config.py.
HIGHPASS_MIC_HZ = 80.0
DENOISE_STRENGTH = 0.5
PEAK_NORMALIZE_DBFS = -3.0           # v3.6.4 default
PEAK_NORMALIZE_MAX_GAIN_DB = 6.0     # v3.6.4 gain cap


def write_wav(path: Path, pcm16: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16)


def rms_db_f(arr: np.ndarray) -> float:
    if arr.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(arr * arr)))
    return 20 * np.log10(max(r, 1e-9))


async def record_session(duration_s: float) -> tuple[bytes, bytes]:
    """Run the live agent's capture + detector pipeline for ``duration_s``
    seconds and return the resulting aligned mic + sys PCM bytes.
    """
    cfg = ConversationConfig(
        joint_silence_close_secs=9999.0,  # disable auto-close
        min_user_total_secs=0.0,           # disable gate
    )
    detector = ConversationDetector(cfg, sample_rate=SR)
    mic = MicCapture(sample_rate=SR, frame_ms=20)
    # SystemCapture has a `system_scope` arg that defaults differently per
    # platform; the default value matches production for each platform.
    sys_cap = SystemCapture(sample_rate=SR, frame_ms=20)

    arm_time = time.monotonic()
    detector.open_session_on_arm(now=arm_time)

    print("Starting mic + system capture...")
    await mic.start()
    await sys_cap.start()

    stop_event = asyncio.Event()

    async def consume(source: str, queue: "asyncio.Queue[tuple[float, np.ndarray]]") -> None:
        while not stop_event.is_set():
            try:
                ts, frame = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            detector.on_frame(source, frame, ts, time.monotonic())

    mic_task = asyncio.create_task(consume("mic", mic.queue))
    sys_task = asyncio.create_task(consume("system", sys_cap.queue))

    print(f"Recording for {duration_s:.0f}s... NOW.")
    print("(Play audio through your speakers + speak a few sentences.)")
    print()
    try:
        # Print a tick every 5 seconds so the user knows it's alive.
        ticks = int(duration_s // 5)
        for i in range(ticks):
            await asyncio.sleep(5.0)
            print(f"  {(i + 1) * 5}s / {duration_s:.0f}s")
        # Remaining
        rem = duration_s - ticks * 5.0
        if rem > 0:
            await asyncio.sleep(rem)
    except KeyboardInterrupt:
        print("  (interrupted — stopping early)")

    stop_event.set()
    await mic.stop()
    await sys_cap.stop()
    await asyncio.gather(mic_task, sys_task, return_exceptions=True)

    detector.commit_close(time.monotonic(), SessionCloseReason.HOTKEY_END)
    buffers = detector.take_closed_session()
    if buffers is None:
        raise RuntimeError("no audio captured — is another sayzo-agent process holding the mic?")
    return bytes(buffers.mic_pcm), bytes(buffers.sys_pcm)


def build_variants(mic_raw: bytes, sys_raw: bytes, out_dir: Path) -> None:
    """Write progressive-pipeline variants for A/B listening."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 00: raw captures (after detector alignment, before any processing).
    write_wav(out_dir / "00_mic_raw.wav", mic_raw)
    write_wav(out_dir / "00_sys_raw.wav", sys_raw)
    print(f"  00_mic_raw.wav             rms={rms_db_f(_i16_to_f32(mic_raw)):>6.2f} dB")
    print(f"  00_sys_raw.wav             rms={rms_db_f(_i16_to_f32(sys_raw)):>6.2f} dB")

    # 01: + AEC (default config: HPF on, NS off, AGC off — matches production).
    cleaned_mic, rep = cancel_echo(mic_raw, sys_raw, SR, AecConfig(enabled=True))
    write_wav(out_dir / "01_aec.wav", cleaned_mic)
    cleaned_f32 = _i16_to_f32(cleaned_mic)
    print(
        f"  01_aec.wav                 rms={rms_db_f(cleaned_f32):>6.2f} dB  "
        f"lag={rep.lag_samples}smp peak={rep.lag_xcorr_peak:.3f}"
    )

    # 02: + dsp.py highpass on the AEC-cleaned mic.
    hpf_f32 = _apply_highpass(cleaned_f32, HIGHPASS_MIC_HZ, SR)
    write_wav(out_dir / "02_aec_hpf.wav", _f32_to_i16(hpf_f32))
    print(f"  02_aec_hpf.wav             rms={rms_db_f(hpf_f32):>6.2f} dB  (+ HPF {HIGHPASS_MIC_HZ:.0f} Hz)")

    # 03: + peak-normalize.
    pknorm_f32 = _peak_normalize(hpf_f32, PEAK_NORMALIZE_DBFS, PEAK_NORMALIZE_MAX_GAIN_DB)
    write_wav(out_dir / "03_aec_hpf_pknorm.wav", _f32_to_i16(pknorm_f32))
    print(f"  03_aec_hpf_pknorm.wav      rms={rms_db_f(pknorm_f32):>6.2f} dB  (+ peak-norm {PEAK_NORMALIZE_DBFS:.0f} dBFS)")

    # 04: full production order — HPF → denoise → peak-norm.
    denoise_f32 = _denoise(hpf_f32, SR, DENOISE_STRENGTH)
    full_f32 = _peak_normalize(denoise_f32, PEAK_NORMALIZE_DBFS, PEAK_NORMALIZE_MAX_GAIN_DB)
    write_wav(out_dir / "04_aec_full_production.wav", _f32_to_i16(full_f32))
    print(
        f"  04_aec_full_production.wav rms={rms_db_f(full_f32):>6.2f} dB  "
        f"(+ noisereduce {DENOISE_STRENGTH}; == live agent output)"
    )

    # 05: same DSP stack WITHOUT AEC — sanity check.
    raw_f32 = _i16_to_f32(mic_raw)
    no_aec_hpf = _apply_highpass(raw_f32, HIGHPASS_MIC_HZ, SR)
    no_aec_denoise = _denoise(no_aec_hpf, SR, DENOISE_STRENGTH)
    no_aec_full = _peak_normalize(no_aec_denoise, PEAK_NORMALIZE_DBFS, PEAK_NORMALIZE_MAX_GAIN_DB)
    write_wav(out_dir / "05_no_aec_full_dsp.wav", _f32_to_i16(no_aec_full))
    print(f"  05_no_aec_full_dsp.wav     rms={rms_db_f(no_aec_full):>6.2f} dB  (DSP without AEC)")

    # Per-second mic RMS table — gives numerical reference alongside the
    # listen test. A sudden jump at one column flags a stage introducing
    # broadband content (e.g., spectral subtraction adding musical noise).
    print()
    print("Per-second mic RMS (dBFS):")
    print(f'{"sec":>4} {"V0_raw":>8} {"V1_aec":>8} {"V2_hpf":>8} {"V3_pknrm":>9} {"V4_full":>8} {"V5_noaec":>9}')
    arrs = {
        "raw": raw_f32,
        "aec": cleaned_f32,
        "hpf": hpf_f32,
        "pknorm": pknorm_f32,
        "full": full_f32,
        "no_aec_full": no_aec_full,
    }
    n = min(len(v) for v in arrs.values())
    total = int(n / SR)
    for s in range(total):
        s0, s1 = s * SR, (s + 1) * SR
        print(
            f"{s:>4} "
            f"{rms_db_f(arrs['raw'][s0:s1]):>8.2f} "
            f"{rms_db_f(arrs['aec'][s0:s1]):>8.2f} "
            f"{rms_db_f(arrs['hpf'][s0:s1]):>8.2f} "
            f"{rms_db_f(arrs['pknorm'][s0:s1]):>9.2f} "
            f"{rms_db_f(arrs['full'][s0:s1]):>8.2f} "
            f"{rms_db_f(arrs['no_aec_full'][s0:s1]):>9.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--duration", type=float, default=30.0, help="Recording duration in seconds (default 30)")
    ap.add_argument("--label", default=None, help="Output subfolder name (default = timestamp)")
    ap.add_argument("--data-dir", default=str(Path.home() / ".sayzo" / "agent"))
    args = ap.parse_args()

    label = args.label or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.data_dir) / "pipeline-lab" / label

    print()
    print(f"Pipeline lab — recording {args.duration:.0f}s session")
    print(f"Output: {out_dir}")
    print()
    print("Prereqs:")
    print("  - No live sayzo-agent process running (would compete for the mic).")
    print("  - Set up speakers + queue audio to play during the recording.")
    print()
    try:
        input("Press Enter to start recording (Ctrl+C to abort)...")
    except KeyboardInterrupt:
        print("\naborted")
        return 1
    print()

    try:
        mic_raw, sys_raw = asyncio.run(record_session(args.duration))
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        return 1

    captured_dur = len(mic_raw) // 2 / SR
    print()
    print(f"Captured {captured_dur:.1f}s mic + {len(sys_raw) // 2 / SR:.1f}s sys.")
    print()
    print("Building pipeline variants...")
    build_variants(mic_raw, sys_raw, out_dir)

    print()
    print(f"Done. WAVs in: {out_dir}")
    print()
    print("Listen in order 00 -> 04. The first variant with the artifact")
    print("(e.g., the static) identifies the stage that introduced it.")
    print("Variant 05 is a sanity check — DSP without AEC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
