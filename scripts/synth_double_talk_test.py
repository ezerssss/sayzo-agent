"""Synthetic double-talk test for capture ceec9db3797a.

Overlays N seconds of your clean speech (sec 28+) onto the loud-echo
region starting at sec 18, then runs the D pipeline (triple-AEC + tighter
echo_guard with coh=0.30) and writes three WAVs you can A/B in Audacity.

The longer the overlay, the more AEC3's adaptive filter gets stuck in a
"frozen during double-talk" state and the worse the post-overlay
cancellation will be. Real-world meeting overlaps are typically 0.5-2 s
(backchannels like "yeah" / "mhm"), so test with shorter values to see
the realistic case.

Usage:
    python scripts/synth_double_talk_test.py [duration_secs]
    duration_secs default = 1.0 (realistic brief overlap)
                  4.0 = stress test (continuous interruption)
"""
from __future__ import annotations
import sys
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sayzo_agent.config import AecConfig, EchoGuardConfig
from sayzo_agent.aec import cancel_echo
from sayzo_agent.echo_guard import (
    classify_buffers,
    zero_out_echo_regions,
    default_speech_detector,
)
from sayzo_agent.models import SessionBuffers, SpeechSegment

SR = 16000


def load_wav_bytes(p: Path) -> bytes:
    with wave.open(str(p), "rb") as w:
        return w.readframes(w.getnframes())


def write_wav(p: Path, b: bytes) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b)


def to_i16(arr: np.ndarray) -> np.ndarray:
    return np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)


def to_f(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def rms_db_f(arr: np.ndarray) -> float:
    if arr.size == 0:
        return -120.0
    return 20 * np.log10(max(float(np.sqrt(np.mean(arr * arr))), 1e-9))


def main() -> int:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    overlay_secs = int(round(dur * SR))
    overlay_start = 18  # sec

    diag = Path.home() / ".sayzo" / "agent" / "aec-diag" / "ceec9db3797a"

    # The existing files were written by the old script naming.
    mic_bytes = load_wav_bytes(diag / "raw_mic.wav")
    sys_bytes = load_wav_bytes(diag / "raw_sys.wav")
    n = min(len(mic_bytes), len(sys_bytes)) // 2 * 2
    mic_f = to_f(mic_bytes[:n])
    sys_f = to_f(sys_bytes[:n])

    # Pull clean user speech from sec 28..28+dur, overlay starting at sec 18.
    overlay_src = mic_f[28 * SR : 28 * SR + overlay_secs].copy()
    print(f"Overlay duration: {dur:.1f} s")
    print(f"Overlay source (sec 28-{28+dur:.1f} clean speech): rms={rms_db_f(overlay_src):.2f} dB")
    print(f"Original mic 18-{18+dur:.1f}s (echo only):         rms={rms_db_f(mic_f[overlay_start*SR:overlay_start*SR+overlay_secs]):.2f} dB")

    synth_mic = mic_f.copy()
    synth_mic[overlay_start * SR : overlay_start * SR + overlay_secs] = (
        synth_mic[overlay_start * SR : overlay_start * SR + overlay_secs] + overlay_src
    )
    print(f"Synthetic double-talk 18-{18+dur:.1f}s:            rms={rms_db_f(synth_mic[overlay_start*SR:overlay_start*SR+overlay_secs]):.2f} dB")

    synth_mic_bytes = to_i16(synth_mic).tobytes()
    sys_pcm_bytes = to_i16(sys_f).tobytes()

    write_wav(diag / "DT_input_before_pipeline.wav", synth_mic_bytes)
    write_wav(diag / "DT_clean_speech_reference.wav", to_i16(overlay_src).tobytes())

    # Triple AEC.
    m = synth_mic_bytes
    for i in range(3):
        m, rep = cancel_echo(m, sys_pcm_bytes, SR, AecConfig(enabled=True))
        print(
            f"  AEC pass {i+1}: peak={rep.lag_xcorr_peak:.3f} "
            f"mic_rms->{rms_db_f(to_f(m)):.2f} dB"
        )

    # Tight echo_guard (D recipe).
    buffers = SessionBuffers()
    buffers.mic_pcm = bytearray(m)
    buffers.sys_pcm = bytearray(sys_pcm_bytes)
    buffers.mic_segments = [
        SpeechSegment("mic", 0.55, 15.43),
        SpeechSegment("mic", 16.16, 25.06),
        SpeechSegment("mic", 25.38, 25.99),
        SpeechSegment("mic", 27.46, 35.01),
        SpeechSegment("mic", 35.52, 36.26),
    ]
    buffers.sys_segments = [SpeechSegment("system", 15.5, 26.5)]

    eg_cfg = EchoGuardConfig(
        subdivide_long_segments_secs=0.3,
        subdivide_window_secs=0.3,
        subdivide_hop_secs=0.1,
        coh_high_threshold=0.30,
    )
    rep = classify_buffers(buffers, SR, eg_cfg, default_speech_detector)
    print(f"echo_guard: dropped={rep.segments_dropped} secs={rep.seconds_dropped:.2f}")
    for r in rep.per_segment:
        for es, ee in r.echo_spans:
            in_overlay = es < (overlay_start + dur) and ee > overlay_start
            marker = " <-- INTERSECTS USER OVERLAY (would chop your voice)" if in_overlay else ""
            print(f"    drop {es:.2f}-{ee:.2f}s{marker}")

    spans = [s for r in rep.per_segment for s in r.echo_spans]
    out_bytes = zero_out_echo_regions(m, spans, SR)
    write_wav(diag / "DT_output_after_pipeline.wav", out_bytes)

    inp_f = to_f(synth_mic_bytes)
    out_f = to_f(out_bytes)
    print()
    print(f'{"sec":>4} {"echo_only":>10} {"+ user":>8} {"after_D":>9}')
    overlay_end_sec = overlay_start + dur
    for s in range(15, 27):
        if overlay_start <= s < overlay_end_sec or (s == int(overlay_start) and dur < 1.0):
            marker = " user overlay"
        elif 16 <= s <= 25:
            marker = " echo only"
        else:
            marker = ""
        print(
            f"{s:>4} {rms_db_f(mic_f[s*SR:(s+1)*SR]):>10.2f} "
            f"{rms_db_f(inp_f[s*SR:(s+1)*SR]):>8.2f} "
            f"{rms_db_f(out_f[s*SR:(s+1)*SR]):>9.2f}{marker}"
        )

    print()
    print(f"Wrote 3 files in {diag}:")
    print("  DT_clean_speech_reference.wav  - 4s of your clean voice (overlay source)")
    print("  DT_input_before_pipeline.wav   - synth mic with your voice mixed onto echo")
    print("  DT_output_after_pipeline.wav   - after D pipeline; did your voice survive?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
