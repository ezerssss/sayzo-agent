"""Diagnose AEC behavior on a real captured session.

Loads `<data_dir>/captures/<id>/audio.opus`, splits it into mic (L) and
sys (R) mono streams, then runs `aec.cancel_echo` with several config
variants and writes the resulting mic-channel WAVs to a sibling
``aec-diag/`` folder so we can A/B by ear.

Usage:
    python scripts/diagnose_aec.py <capture_id> [--data-dir DIR]

Important: the mic + sys decoded from the Opus are NOT the raw captured
audio — by the time they land in the Opus the live pipeline has already
run AEC + DSP on the mic channel and light HPF on the sys channel. So
the ``mic.wav`` written here is what Deepgram actually heard from the
upload, and any further variant is AEC running ON TOP of an already-
processed mic. Useful for residual analysis but DO NOT treat the
variants as equivalent to "what would a different first-pass AEC have
produced" — that comparison requires recording with the candidate config
on the live agent.

Variants written (mic only — sys is always the reference, unchanged):
    mic.wav             — mic channel decoded from the Opus (= what's
                          actually in the uploaded file, post-live-AEC
                          + post-DSP + post-Opus-roundtrip)
    sys.wav             — sys channel decoded from the Opus
    default.wav         — second AEC pass at current AecConfig defaults
    ns_off_hpf_off.wav  — pure AEC3 with NS/HPF disabled
    ns_off_hpf_on.wav   — explicit NS off, HPF on (same as the
                          v3.5.2 final default — sanity-check shape)
    ns_on_hpf_off.wav   — NS3 alone (in case static comes from HPF)
    lag_search_200.wav  — pre-v3.5.2 search window
    lag_search_800.wav  — wider search than current 500 ms
    forced_lag_*.wav    — bypass the trust check, feed AEC3 the raw
                          xcorr-estimated lag even when peak < threshold.
                          Tests whether the lag estimator IS finding the
                          right peak but we're rejecting it as untrusted.

Reads `record.json` to print the original aec metadata for context.
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

# Windows cp1252 console can't render Δ etc.; force UTF-8 for output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Add repo root to path so we can import sayzo_agent
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sayzo_agent.aec import cancel_echo, AecReport
from sayzo_agent.config import AecConfig


SR = 16000


def _decode_opus_stereo(opus_path: Path) -> tuple[bytes, bytes]:
    """Decode the stereo Opus into (mic_pcm16, sys_pcm16) mono bytes.

    Uses PyAV (already a project dep). The Opus file was encoded by
    sink.encode_opus_stereo with L=mic, R=sys at 16 kHz.
    """
    import av

    container = av.open(str(opus_path))
    stream = container.streams.audio[0]
    print(f"  opus codec internal: sr={stream.codec_context.sample_rate} "
          f"channels={stream.codec_context.channels} "
          f"layout={stream.codec_context.layout.name} "
          f"(decoder will resample to {SR} Hz mono per channel)")

    mic_chunks: list[np.ndarray] = []
    sys_chunks: list[np.ndarray] = []
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=SR)

    for packet in container.demux(stream):
        for frame in packet.decode():
            for r_frame in resampler.resample(frame):
                arr = r_frame.to_ndarray()  # shape (channels, samples) or (1, samples*ch) interleaved
                # PyAV layout=stereo with s16 returns (1, n*2) interleaved when format is packed.
                # Reshape to (n, 2).
                if arr.ndim == 2 and arr.shape[0] == 1:
                    flat = arr.flatten()
                    n = flat.size // 2
                    interleaved = flat[: n * 2].reshape(n, 2)
                    mic_chunks.append(interleaved[:, 0].copy())
                    sys_chunks.append(interleaved[:, 1].copy())
                elif arr.ndim == 2 and arr.shape[0] == 2:
                    mic_chunks.append(arr[0].copy())
                    sys_chunks.append(arr[1].copy())
                else:
                    raise RuntimeError(f"unexpected frame shape: {arr.shape}")
    # Flush
    for r_frame in resampler.resample(None):
        arr = r_frame.to_ndarray()
        if arr.ndim == 2 and arr.shape[0] == 1:
            flat = arr.flatten()
            n = flat.size // 2
            interleaved = flat[: n * 2].reshape(n, 2)
            mic_chunks.append(interleaved[:, 0].copy())
            sys_chunks.append(interleaved[:, 1].copy())
        elif arr.ndim == 2 and arr.shape[0] == 2:
            mic_chunks.append(arr[0].copy())
            sys_chunks.append(arr[1].copy())

    mic = np.concatenate(mic_chunks).astype(np.int16)
    sys_arr = np.concatenate(sys_chunks).astype(np.int16)
    return mic.tobytes(), sys_arr.tobytes()


def _write_wav(path: Path, pcm16: bytes, sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16)


def _rms_db(pcm16: bytes) -> float:
    arr = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64) / 32768.0
    if arr.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    return 20 * np.log10(max(rms, 1e-9))


def _summary(name: str, mic_in: bytes, mic_out: bytes, rep: AecReport) -> None:
    """One-line per-variant summary."""
    delta = _rms_db(mic_out) - _rms_db(mic_in)
    print(
        f"  {name:32s} ran={str(rep.ran):5s} "
        f"frames={rep.frames_processed:5d} dur={rep.duration_ms:6.0f}ms "
        f"lag={rep.lag_samples:+5d} peak={rep.lag_xcorr_peak:.3f} "
        f"rms_in={_rms_db(mic_in):6.2f}dB rms_out={_rms_db(mic_out):6.2f}dB "
        f"delta={delta:+5.2f}dB"
    )


def _force_lag_variant(
    mic_pcm: bytes,
    sys_pcm: bytes,
    forced_lag: int,
    label: str,
) -> tuple[bytes, AecReport]:
    """Bypass the trust check and feed AEC3 the forced lag.

    Constructs a cfg with min_xcorr_peak=0 so the trust check is a no-op,
    AND lag_max_ms large enough to accept the forced lag. This isolates
    "is the xcorr lag right but we're rejecting it" from "is the xcorr
    lag wrong."

    Implementation note: we can't easily inject a hand-picked lag into
    cancel_echo without modifying it, so this variant uses a config that
    forces the existing pipeline to accept whatever lag the estimator
    returns even at low peak. The forced_lag arg is informational —
    we just relax the gates and see what happens.
    """
    cfg = AecConfig(
        enabled=True,
        noise_suppression=False,
        high_pass_filter=False,
        min_xcorr_peak=0.0,
        lag_max_ms=2000,
    )
    return cancel_echo(mic_pcm, sys_pcm, SR, cfg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture_id")
    ap.add_argument("--data-dir", default=str(Path.home() / ".sayzo" / "agent"))
    ap.add_argument("--out", default=None,
                    help="Output dir (default: <data_dir>/aec-diag/<capture_id>/)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    cap_dir = data_dir / "captures" / args.capture_id
    opus_path = cap_dir / "audio.opus"
    record_path = cap_dir / "record.json"

    if not opus_path.exists():
        print(f"FAIL: {opus_path} not found")
        return 1

    out_dir = Path(args.out) if args.out else (
        data_dir / "aec-diag" / args.capture_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Capture: {args.capture_id}")
    print(f"Source:  {opus_path}")
    print(f"Output:  {out_dir}")

    # Show what the agent recorded about this session.
    if record_path.exists():
        rec = json.loads(record_path.read_text())
        print(f"  title:   {rec.get('title', '<no title>')}")
        print(f"  meta.aec: {json.dumps(rec.get('metadata', {}).get('aec', {}), indent=2)}")

    print()
    print("Decoding opus...")
    mic_pcm, sys_pcm = _decode_opus_stereo(opus_path)
    duration_s = len(mic_pcm) / 2 / SR
    print(f"  duration: {duration_s:.1f}s ({len(mic_pcm)//2} samples per channel)")
    print()

    # Write the Opus-decoded channels as A/B reference. These are NOT raw
    # capture — the live pipeline already ran AEC + DSP on the mic before
    # encoding, so ``mic.wav`` is what Deepgram heard.
    _write_wav(out_dir / "mic.wav", mic_pcm)
    _write_wav(out_dir / "sys.wav", sys_pcm)
    print(f"  mic RMS (decoded from Opus): {_rms_db(mic_pcm):.2f} dB")
    print(f"  sys RMS (decoded from Opus): {_rms_db(sys_pcm):.2f} dB")
    print()

    print("Running variants...")

    variants: list[tuple[str, AecConfig]] = [
        ("default (post-NS-revert)",
         AecConfig(enabled=True)),
        ("ns_off_hpf_off (pure AEC3)",
         AecConfig(enabled=True, noise_suppression=False, high_pass_filter=False)),
        ("ns_on_hpf_off (NS3 alone)",
         AecConfig(enabled=True, noise_suppression=True, high_pass_filter=False)),
        ("lag_search_200 (pre-v3.5.2)",
         AecConfig(enabled=True, lag_search_ms=200, lag_max_ms=200,
                   noise_suppression=False, high_pass_filter=False)),
        ("lag_search_800",
         AecConfig(enabled=True, lag_search_ms=800, lag_max_ms=800,
                   noise_suppression=False, high_pass_filter=False)),
        ("low_xcorr_trust (peak floor=0)",
         AecConfig(enabled=True, min_xcorr_peak=0.0,
                   noise_suppression=False, high_pass_filter=False)),
        ("low_xcorr + wider search",
         AecConfig(enabled=True, lag_search_ms=800, lag_max_ms=800,
                   min_xcorr_peak=0.0,
                   noise_suppression=False, high_pass_filter=False)),
    ]

    for label, cfg in variants:
        cleaned, rep = cancel_echo(mic_pcm, sys_pcm, SR, cfg)
        slug = (
            label.lower()
            .replace(" ", "_")
            .replace("(", "")
            .replace(")", "")
            .replace("/", "_")
            .replace("=", "")
            .replace(",", "")
            .replace("+", "_")
            .replace(".", "_")
        )
        _write_wav(out_dir / f"{slug}.wav", cleaned)
        _summary(label, mic_pcm, cleaned, rep)

    # Per-time-window xcorr scan — finds whether mic/sys correlate
    # locally even when the global xcorr is dead. If we see high peaks
    # in some windows, the relationship is non-stationary (mic moved,
    # echo path drifted). If every window has peak < 0.1, the mic
    # genuinely doesn't contain sys content.
    print()
    print("Per-window xcorr scan (1.0 s windows, ±500 ms search):")
    print("  window_start  best_lag(ms)   peak")
    mic_arr = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    sys_arr = np.frombuffer(sys_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    win_n = SR  # 1 s
    search_n = SR // 2  # 500 ms each way
    from sayzo_agent.echo_guard import estimate_delay
    best_window_peak = 0.0
    best_window_info = None
    for ws in range(0, len(mic_arr) - win_n, SR * 5):  # every 5 s
        we = ws + win_n
        mic_w = mic_arr[ws:we]
        # mic-anchor at search_n inside sys_wide so search ±search_n is testable
        sys_lo_want = ws - search_n
        sys_hi_want = ws + win_n + search_n
        sys_lo_actual = max(0, sys_lo_want)
        sys_hi_actual = min(len(sys_arr), sys_hi_want)
        pad_left = sys_lo_actual - sys_lo_want
        pad_right = sys_hi_want - sys_hi_actual
        sys_w = np.concatenate(
            [
                np.zeros(pad_left, dtype=np.float32),
                sys_arr[sys_lo_actual:sys_hi_actual],
                np.zeros(pad_right, dtype=np.float32),
            ]
        )
        if len(sys_w) < len(mic_w):
            continue
        lag, peak = estimate_delay(mic_w, sys_w, search_n, search_n)
        lag_ms = lag * 1000.0 / SR
        # Per-window mic + sys energy for context.
        mic_e = float(np.sqrt(np.mean(mic_w * mic_w)))
        sys_e = float(np.sqrt(np.mean(sys_arr[ws:we] ** 2))) if we <= len(sys_arr) else 0.0
        print(
            f"  {ws/SR:6.1f}s        {lag_ms:+7.1f}ms     {peak:.3f}    "
            f"mic_rms={20*np.log10(max(mic_e,1e-9)):6.2f}dB "
            f"sys_rms={20*np.log10(max(sys_e,1e-9)):6.2f}dB"
        )
        if peak > best_window_peak:
            best_window_peak = peak
            best_window_info = (ws / SR, lag_ms, peak)

    print()
    if best_window_info:
        ws, lag_ms, peak = best_window_info
        print(
            f"Strongest local correlation: window @ {ws:.1f}s, "
            f"lag={lag_ms:+.1f}ms, peak={peak:.3f}"
        )
        if peak < 0.10:
            print(
                "  → All windows below the 0.10 trust threshold. The mic "
                "signal in this capture does NOT contain a linear-filter "
                "image of sys. Possibilities:\n"
                "    a) No actual speaker bleed (e.g. user wore headphones)\n"
                "    b) Heavy non-linear distortion broke the linear path\n"
                "    c) The 'echo' the user heard was stereo playback of "
                "the sys channel, not mic-side bleed"
            )
        else:
            print(
                "  → Some windows have real correlation. The session has a "
                "time-varying echo path that one-shot global lag can't track."
            )

    print()
    print(f"Done. Listen to WAVs in:  {out_dir}")
    print(
        "Suggested A/B in Audacity: import mic.wav as one track, "
        "each variant on its own. Solo to compare."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
