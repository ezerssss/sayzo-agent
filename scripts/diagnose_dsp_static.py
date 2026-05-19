"""Diagnose which session-close DSP stage introduces faint static into the
mic side of captures.

User-reported symptom (v3.6.x): production-encoded Opus has faint static on
the mic channel; the prototype outputs (``diagnose_aec.py``, ``synth_double
_talk_test.py``) do NOT have static. Those prototypes skip ``dsp.py``
entirely — they write WAVs straight after AEC + echo_guard. So the static
must enter somewhere in ``dsp.py::apply_mic_dsp``.

The chain is:

    decoded_mic → AEC pass → HPF (80 Hz Butter order 4) → noisereduce
                  (stationary, prop_decrease=0.5) → peak_normalize (-1 dBFS)

This script writes four WAVs that progressively stack the DSP stages on top
of a fresh AEC pass against the captured Opus's mic + sys. The user listens
in order; the first variant where static is audible identifies which stage
introduced it.

Variants written to ``<data_dir>/aec-diag/<id>/dsp/``:

    1_aec_only.wav         - post-AEC mic, no DSP
    2_aec_hpf.wav          - + 80 Hz Butterworth highpass
    3_aec_hpf_pknorm.wav   - + peak-normalize (-1 dBFS)
    4_aec_full.wav         - + noisereduce (prop_decrease=0.5)
                              == current production DSP chain

Usage:
    python scripts/diagnose_dsp_static.py <capture_id> [--data-dir DIR]

Decision tree:
    static appears at 4 only        → noisereduce is the culprit
    static appears at 3 but not 2   → peak-normalize amplifying residuals
    static appears at 2 but not 1   → highpass stacking with AEC's HPF
    static appears at 1             → AEC itself (contradicts prototype evidence)
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# Force UTF-8 stdout so Windows cp1252 console doesn't choke on the table.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.diagnose_aec import _decode_opus_stereo  # reuse the Opus reader
from sayzo_agent.aec import cancel_echo
from sayzo_agent.config import AecConfig
from sayzo_agent.dsp import (
    _apply_highpass,
    _denoise,
    _f32_to_i16,
    _i16_to_f32,
    _peak_normalize,
)

SR = 16000

# Production defaults — mirror what CaptureConfig ships with so the listen
# test exactly reproduces the live pipeline behavior.
HIGHPASS_MIC_HZ = 80.0
DENOISE_STRENGTH = 0.5
PEAK_NORMALIZE_DBFS = -1.0


def _write_wav(path: Path, pcm16: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16)


def _rms_db(arr: np.ndarray) -> float:
    if arr.size == 0:
        return -120.0
    r = float(np.sqrt(np.mean(arr * arr)))
    return 20 * np.log10(max(r, 1e-9))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture_id")
    ap.add_argument("--data-dir", default=str(Path.home() / ".sayzo" / "agent"))
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    cap_dir = data_dir / "captures" / args.capture_id
    opus_path = cap_dir / "audio.opus"
    if not opus_path.exists():
        print(f"FAIL: {opus_path} not found")
        return 1

    out_dir = data_dir / "aec-diag" / args.capture_id / "dsp"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Capture: {args.capture_id}")
    print(f"Source:  {opus_path}")
    print(f"Output:  {out_dir}")
    print()

    print("Decoding Opus...")
    mic_bytes, sys_bytes = _decode_opus_stereo(opus_path)
    n = min(len(mic_bytes), len(sys_bytes)) // 2 * 2
    mic_bytes, sys_bytes = mic_bytes[:n], sys_bytes[:n]
    print(f"  duration: {n // 2 / SR:.1f}s")
    print()

    # ---- Step 1: fresh AEC pass on the decoded buffers ---------------------
    print("Running fresh AEC pass on decoded buffers...")
    cleaned_mic, rep = cancel_echo(mic_bytes, sys_bytes, SR, AecConfig(enabled=True))
    cleaned_f32 = _i16_to_f32(cleaned_mic)
    print(
        f"  AEC: lag={rep.lag_samples}smp peak={rep.lag_xcorr_peak:.3f} "
        f"mic_rms {_rms_db(_i16_to_f32(mic_bytes)):.2f} -> {_rms_db(cleaned_f32):.2f}dB"
    )
    print()

    # ---- Variants build progressively from the same AEC output -------------
    print("Building DSP variants...")

    v1_f32 = cleaned_f32
    _write_wav(out_dir / "1_aec_only.wav", _f32_to_i16(v1_f32))
    print(f"  1_aec_only.wav         rms={_rms_db(v1_f32):>6.2f}dB")

    v2_f32 = _apply_highpass(v1_f32, HIGHPASS_MIC_HZ, SR)
    _write_wav(out_dir / "2_aec_hpf.wav", _f32_to_i16(v2_f32))
    print(f"  2_aec_hpf.wav          rms={_rms_db(v2_f32):>6.2f}dB  (+ HPF {HIGHPASS_MIC_HZ:.0f} Hz)")

    v3_f32 = _peak_normalize(v2_f32, PEAK_NORMALIZE_DBFS)
    _write_wav(out_dir / "3_aec_hpf_pknorm.wav", _f32_to_i16(v3_f32))
    print(f"  3_aec_hpf_pknorm.wav   rms={_rms_db(v3_f32):>6.2f}dB  (+ peak-norm to {PEAK_NORMALIZE_DBFS:.0f} dBFS)")

    # Production order: HPF -> denoise -> peak-normalize. We test denoise
    # AFTER peak-normalize here only because that's the order the variants
    # stack; in production denoise runs on the post-HPF pre-pknorm signal.
    # To match production exactly, denoise then re-peak-normalize.
    v4_pre_denoise = v2_f32  # post-HPF, pre-pknorm signal that production hits
    v4_after_denoise = _denoise(v4_pre_denoise, SR, DENOISE_STRENGTH)
    v4_f32 = _peak_normalize(v4_after_denoise, PEAK_NORMALIZE_DBFS)
    _write_wav(out_dir / "4_aec_full.wav", _f32_to_i16(v4_f32))
    print(
        f"  4_aec_full.wav         rms={_rms_db(v4_f32):>6.2f}dB  "
        f"(+ noisereduce prop_decrease={DENOISE_STRENGTH}; == production DSP)"
    )
    print()

    # ---- Per-second RMS table for numerical reference ----------------------
    print("Per-second mic RMS (dBFS):")
    print(f'{"sec":>4} {"V1_aec":>8} {"V2_hpf":>8} {"V3_pknrm":>9} {"V4_full":>8}')
    total = int(min(len(v1_f32), len(v2_f32), len(v3_f32), len(v4_f32)) / SR)
    for s in range(0, total):
        s0, s1 = s * SR, (s + 1) * SR
        print(
            f"{s:>4} "
            f"{_rms_db(v1_f32[s0:s1]):>8.2f} "
            f"{_rms_db(v2_f32[s0:s1]):>8.2f} "
            f"{_rms_db(v3_f32[s0:s1]):>9.2f} "
            f"{_rms_db(v4_f32[s0:s1]):>8.2f}"
        )

    print()
    print("Listen in Audacity in numeric order (1 -> 4). The first variant")
    print("where you hear static identifies the culprit stage.")
    print()
    print("  Static at 4 only       -> noisereduce")
    print("  Static at 3 but not 2  -> peak-normalize amplifying residuals")
    print("  Static at 2 but not 1  -> highpass stacking with AEC's HPF")
    print("  Static at 1            -> AEC itself (revisit; should not happen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
