"""Diagnose / A-B the inter-channel loudness match on a REAL capture.

Loads ``<data_dir>/captures/<id>/audio.opus`` (mic=L, sys=R), measures the
PERCEIVED loudness (LUFS) of each channel, runs ``loudness.match_loudness``
(the v3.22 session-close stage), and reports the before/after loudness gap so
you can hear + see whether the two sides of the conversation end balanced. This
is the tuning tool the plan calls for: it works on shipped captures with no
re-recording.

Note: the channels decoded from the Opus already went through the live
pipeline's per-channel processing, so the "before" gap here is representative of
what replay sounds like today. Re-running the match on top of them shows the
balance the v3.22 stage delivers.

Usage:
    python scripts/diagnose_loudness.py <capture_id> [--data-dir DIR]
        [--method lufs|rms] [--solo-target -18] [--max-boost 6] [--ceiling -1]

Writes to ``<data_dir>/loudness-diag/<id>/``:
    orig_mic.wav / orig_sys.wav        - channels as decoded (before)
    matched_mic.wav / matched_sys.wav  - after match_loudness
    orig_stereo.wav / matched_stereo.wav - L=mic R=sys, for the balance A/B
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.diagnose_aec import _decode_opus_stereo  # reuse the Opus reader
from sayzo_agent.config import CaptureConfig
from sayzo_agent.loudness import match_loudness

SR = 16000


def _write_wav(path: Path, pcm16: bytes, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16)


def _interleave(mic: bytes, sys: bytes) -> bytes:
    n = min(len(mic), len(sys)) // 2
    m = np.frombuffer(mic[: n * 2], dtype=np.int16)
    s = np.frombuffer(sys[: n * 2], dtype=np.int16)
    out = np.empty(n * 2, dtype=np.int16)
    out[0::2] = m
    out[1::2] = s
    return out.tobytes()


def _lufs(pcm16: bytes) -> float:
    try:
        import pyloudnorm as pyln
    except Exception:
        return float("nan")
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size < int(0.4 * SR) + 1:
        return float("-inf")
    try:
        return float(pyln.Meter(SR).integrated_loudness(x))
    except Exception:
        return float("-inf")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture_id")
    ap.add_argument("--data-dir", default=str(Path.home() / ".sayzo" / "agent"))
    ap.add_argument("--method", choices=["lufs", "rms"], default="lufs")
    ap.add_argument("--target", type=float, default=-18.0,
                    help="loudness target/floor LUFS (loudness_target_lufs)")
    ap.add_argument("--max-boost", type=float, default=6.0)
    ap.add_argument("--ceiling", type=float, default=-3.0)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    opus_path = data_dir / "captures" / args.capture_id / "audio.opus"
    if not opus_path.exists():
        print(f"FAIL: {opus_path} not found")
        return 1

    out_dir = data_dir / "loudness-diag" / args.capture_id
    print(f"Capture: {args.capture_id}")
    print(f"Source:  {opus_path}")
    print(f"Output:  {out_dir}")
    print()

    print("Decoding Opus...")
    mic, sys_pcm = _decode_opus_stereo(opus_path)
    n = min(len(mic), len(sys_pcm)) // 2 * 2
    mic, sys_pcm = mic[:n], sys_pcm[:n]
    print(f"  duration: {n // 2 / SR:.1f}s")
    print()

    cfg = CaptureConfig(
        loudness_match_enabled=True,
        loudness_method=args.method,
        loudness_target_lufs=args.target,
        loudness_max_boost_db=args.max_boost,
        loudness_peak_ceiling_dbfs=args.ceiling,
    )

    mic_before, sys_before = _lufs(mic), _lufs(sys_pcm)
    mic_m, sys_m, rep = match_loudness(mic, sys_pcm, SR, cfg)
    mic_after, sys_after = _lufs(mic_m), _lufs(sys_m)

    def _f(v: float) -> str:
        return "  (silent)" if v == float("-inf") else f"{v:8.2f}"

    print("Loudness (LUFS, lower = quieter):")
    print(f'  {"":8} {"mic":>10} {"sys":>10} {"gap":>8}')
    gap_b = abs(mic_before - sys_before) if np.isfinite(mic_before) and np.isfinite(sys_before) else float("nan")
    gap_a = abs(mic_after - sys_after) if np.isfinite(mic_after) and np.isfinite(sys_after) else float("nan")
    print(f"  before  {_f(mic_before)} {_f(sys_before)}  {gap_b:7.2f}")
    print(f"  after   {_f(mic_after)} {_f(sys_after)}  {gap_a:7.2f}")
    print()
    print(f"  method={rep.method}{' (fallback!)' if rep.fallback_used else ''} "
          f"target={rep.common_target_lufs:.2f} "
          f"gains mic{rep.mic_gain_db:+.2f} sys{rep.sys_gain_db:+.2f} "
          f"joint_atten={rep.joint_attenuation_db:.2f} "
          f"{'[mic-only]' if rep.mic_only else ''}")
    print()

    _write_wav(out_dir / "orig_mic.wav", mic)
    _write_wav(out_dir / "orig_sys.wav", sys_pcm)
    _write_wav(out_dir / "matched_mic.wav", mic_m)
    _write_wav(out_dir / "matched_sys.wav", sys_m)
    _write_wav(out_dir / "orig_stereo.wav", _interleave(mic, sys_pcm), channels=2)
    _write_wav(out_dir / "matched_stereo.wav", _interleave(mic_m, sys_m), channels=2)
    print(f"WAVs written to {out_dir}")
    print("A/B orig_stereo.wav vs matched_stereo.wav — the matched one should")
    print("have the user's voice (L) and the far side (R) at the same level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
