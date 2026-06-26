"""WebRTC AEC3 pre-pass — subtracts speaker bleed from the mic channel
before echo_guard's per-segment classifier runs.

The existing ``echo_guard`` is a *classifier*: it can drop a whole VAD
segment that looks like pure echo, but it cannot subtract a speaker-bleed
signal from a mic segment that also contains real user speech (the
classic double-talk failure mode that motivated this module). AEC3
performs the actual sample-level subtraction with an adaptive filter
learning the room/speaker/mic impulse response from the reference
channel.

Pipeline-wise this runs at session close, on the heavy-worker executor,
*before* ``echo_guard.classify_buffers``. echo_guard then operates on an
already-cleaned mic and serves as the non-linear residual safety net
(cheap-laptop speaker driver compression, BT codec re-encoding,
reverb tails that exceed AEC3's impulse-response window).

Frame contract: WebRTC AEC3 (via ``livekit.rtc.apm``) demands exactly
10 ms blocks at 8/16/32/48 kHz. We pass 16 kHz mono everywhere else in
the pipeline (Deepgram target), so frames are 160 int16 samples each.

The mic and sys streams arrive on independent device clocks (sounddevice
mic vs WASAPI loopback on Windows / Process Tap helper on macOS); a
global lag of tens of ms is normal and would prevent AEC3 from
converging quickly. We reuse :func:`echo_guard.estimate_delay` to find
the bulk lag once per session, pre-shift the sys buffer, and pass
``set_stream_delay_ms(0)`` so AEC3's internal delay tracker only has to
absorb residual per-frame jitter.

Off by default in v3.4.0 (``SAYZO_AEC__ENABLED=1`` to enable for
dogfooding); the v3.4.1 release flips the default ON once captures
have been validated on speakers on both platforms.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import echo_guard
from .config import AecConfig

log = logging.getLogger(__name__)


_NUM_CHANNELS = 1
_LAG_WINDOW_SECS = 5.0


# Lazy load of the livekit APM. Same pattern as ``dsp._get_noisereduce``
# — keep the heavy native binary out of the agent's boot path so idle
# memory stays where v2.14 put it. _SENTINEL distinguishes "not yet
# tried" from "tried, failed" so we log the failure exactly once.
_SENTINEL = object()
_apm_pair: Any = _SENTINEL


def _get_apm():
    """Returns (AudioProcessingModule, AudioFrame) or None on import
    failure. Logged once at warning level."""
    global _apm_pair
    if _apm_pair is not _SENTINEL:
        return _apm_pair
    try:
        from livekit.rtc.apm import AudioProcessingModule
        from livekit.rtc import AudioFrame
        _apm_pair = (AudioProcessingModule, AudioFrame)
    except Exception as e:
        log.warning(
            "[aec] livekit.rtc.apm unavailable (%s); AEC disabled for this session", e,
        )
        _apm_pair = None
    return _apm_pair


@dataclass
class AecReport:
    """Per-session summary for logs + record.json metadata."""
    enabled: bool
    ran: bool = False
    skip_reason: str = ""
    lag_samples: int = 0
    lag_xcorr_peak: float = 0.0
    frames_processed: int = 0
    duration_ms: float = 0.0
    mic_rms_before: float = 0.0
    mic_rms_after: float = 0.0
    sys_rms: float = 0.0


def cancel_echo(
    mic_pcm16: bytes,
    sys_pcm16: bytes,
    sr: int,
    cfg: AecConfig,
) -> tuple[bytes, AecReport]:
    """Linear AEC over the mic channel using sys as the reference.

    Returns ``(cleaned_mic_pcm16, report)``. Pure: never mutates inputs.
    On any failure path (disabled, channel silent, livekit unavailable,
    runtime error) returns the input mic bytes unchanged with
    ``skip_reason`` populated on the report.
    """
    report = AecReport(enabled=cfg.enabled)

    if not cfg.enabled:
        report.skip_reason = "disabled"
        return mic_pcm16, report

    if not mic_pcm16 or not sys_pcm16:
        report.skip_reason = "empty_buffers"
        return mic_pcm16, report

    # AEC3 only supports these sample rates. We pass 16 kHz from the
    # rest of the pipeline; the guard is here for defensive completeness.
    if sr not in (8000, 16000, 32000, 48000):
        report.skip_reason = f"unsupported_sr_{sr}"
        return mic_pcm16, report

    apm_pair = _get_apm()
    if apm_pair is None:
        report.skip_reason = "livekit_unavailable"
        return mic_pcm16, report
    APM, AudioFrame = apm_pair

    samples_per_frame = sr // 100  # 10 ms

    mic_int16 = np.frombuffer(mic_pcm16, dtype=np.int16)
    sys_int16 = np.frombuffer(sys_pcm16, dtype=np.int16)

    mic_f32 = mic_int16.astype(np.float32) / 32768.0
    sys_f32 = sys_int16.astype(np.float32) / 32768.0

    mic_rms = float(np.sqrt(np.mean(mic_f32 * mic_f32))) if mic_f32.size else 0.0
    sys_rms = float(np.sqrt(np.mean(sys_f32 * sys_f32))) if sys_f32.size else 0.0
    report.mic_rms_before = mic_rms
    report.sys_rms = sys_rms

    if mic_rms < cfg.min_mic_rms:
        report.skip_reason = "mic_silent"
        return mic_pcm16, report
    if sys_rms < cfg.min_sys_rms:
        # No echo possible without speaker output; bail.
        report.skip_reason = "sys_silent"
        return mic_pcm16, report

    # Global mic↔sys lag via echo_guard's xcorr. Operates on a high-
    # energy window so silences don't dominate the correlation.
    lag, peak = _estimate_global_lag(mic_f32, sys_f32, sr, cfg)
    report.lag_samples = lag
    report.lag_xcorr_peak = peak

    if peak < cfg.min_xcorr_peak:
        # Couldn't find a confident lag — fall back to 0 and let
        # AEC3's internal delay tracker take it from there.
        lag = 0
    elif abs(lag) > int(cfg.lag_max_ms * sr / 1000):
        log.info(
            "[aec] lag estimate %+d samples (%.1fms) exceeds cap %dms; falling back to 0",
            lag, lag * 1000.0 / sr, cfg.lag_max_ms,
        )
        lag = 0

    sys_aligned = _align_reference(sys_int16, lag, len(mic_int16))

    # AGC stays False — it dynamically pumps mic gain across the
    # session, which is right for a phone call but wrong for our
    # pipeline (would boost ambient noise to speech level during
    # far-side monologue, confusing Deepgram diarize). Final level is
    # set after session trim by loudness.match_loudness (v3.22+ default);
    # dsp.py's peak-normalize is the LOUDNESS_MATCH_ENABLED=0 fallback.
    apm = APM(
        echo_cancellation=True,
        noise_suppression=bool(cfg.noise_suppression),
        high_pass_filter=bool(cfg.high_pass_filter),
        auto_gain_control=False,
    )
    apm.set_stream_delay_ms(0)

    t0 = time.monotonic()
    n_total = len(mic_int16)
    n_full_frames = n_total // samples_per_frame
    out_buf = np.empty(n_full_frames * samples_per_frame, dtype=np.int16)

    try:
        for i in range(n_full_frames):
            s = i * samples_per_frame
            e = s + samples_per_frame

            ref_frame = AudioFrame(
                sys_aligned[s:e].tobytes(),
                sample_rate=sr,
                num_channels=_NUM_CHANNELS,
                samples_per_channel=samples_per_frame,
            )
            apm.process_reverse_stream(ref_frame)

            mic_frame = AudioFrame(
                mic_int16[s:e].tobytes(),
                sample_rate=sr,
                num_channels=_NUM_CHANNELS,
                samples_per_channel=samples_per_frame,
            )
            apm.process_stream(mic_frame)

            out_buf[s:e] = np.frombuffer(mic_frame.data, dtype=np.int16)
    except Exception as e:
        log.warning("[aec] APM frame loop failed (%s); falling back to raw mic", e, exc_info=True)
        report.skip_reason = "apm_error"
        return mic_pcm16, report

    # Partial last frame (less than 10 ms): can't run through APM, keep raw.
    tail = mic_int16[n_full_frames * samples_per_frame :]
    if tail.size:
        out_pcm = np.concatenate([out_buf, tail])
    else:
        out_pcm = out_buf

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    out_bytes = out_pcm.tobytes()

    report.ran = True
    report.frames_processed = n_full_frames
    report.duration_ms = elapsed_ms
    out_f32 = out_pcm.astype(np.float32) / 32768.0
    report.mic_rms_after = float(
        np.sqrt(np.mean(out_f32 * out_f32))
    ) if out_f32.size else 0.0

    return out_bytes, report


def _align_reference(sys_int16: np.ndarray, lag: int, target_len: int) -> np.ndarray:
    """Shift sys so sample i aligns with mic sample i.

    ``lag > 0`` means the mic captures echo of sys played ``lag``
    samples earlier (echo travel time). To align, sys is delayed by
    inserting ``lag`` zero samples at the front. ``lag < 0`` would mean
    sys arrives later than mic at the OS layer (rare — driver buffering
    on the loopback side); drop the first ``|lag|`` samples.

    Pads or truncates to ``target_len`` so the per-frame loop's indexing
    is always safe.
    """
    if lag > 0:
        out = np.concatenate(
            [np.zeros(lag, dtype=np.int16), sys_int16]
        )
    elif lag < 0:
        out = sys_int16[-lag:]
    else:
        out = sys_int16

    if len(out) < target_len:
        out = np.concatenate(
            [out, np.zeros(target_len - len(out), dtype=np.int16)]
        )
    elif len(out) > target_len:
        out = out[:target_len]
    return out


def _estimate_global_lag(
    mic_f32: np.ndarray,
    sys_f32: np.ndarray,
    sr: int,
    cfg: AecConfig,
) -> tuple[int, float]:
    """One-shot global lag estimate via :func:`echo_guard.estimate_delay`.

    Picks the highest-energy mic window of ~``_LAG_WINDOW_SECS`` so the
    correlation isn't dominated by long silences (which produce random
    near-zero peaks). Falls back to ``(0, 0.0)`` for sessions too short
    to give a reliable estimate.
    """
    n_mic = len(mic_f32)
    if n_mic == 0 or len(sys_f32) == 0:
        return 0, 0.0

    win = min(n_mic, int(_LAG_WINDOW_SECS * sr))
    if win < 1024:
        return 0, 0.0

    search_samples = max(1, int(cfg.lag_search_ms * sr / 1000))

    # Find the highest-energy mic window. Coarse stride is fine — we
    # just need ANY high-energy window, not the absolute peak.
    stride = max(1024, win // 4)
    best_start = 0
    best_energy = 0.0
    last_start = max(0, n_mic - win)
    for s in range(0, last_start + 1, stride):
        e = s + win
        if e > n_mic:
            break
        energy = float(np.sum(mic_f32[s:e] * mic_f32[s:e]))
        if energy > best_energy:
            best_energy = energy
            best_start = s

    mic_win = mic_f32[best_start:best_start + win]

    # Build sys_wide so the full ±search_samples lag range is testable.
    # That means sys_wide must extend search_samples beyond the mic
    # window on BOTH sides; we zero-pad where sys doesn't have data
    # (e.g. session start, session end). Padding never moves the
    # mic-anchor — it's always at position search_samples inside
    # sys_wide.
    sys_lo = best_start - search_samples
    sys_hi = best_start + win + search_samples
    sys_lo_clamped = max(0, sys_lo)
    sys_hi_clamped = min(len(sys_f32), sys_hi)
    pad_left = sys_lo_clamped - sys_lo
    pad_right = sys_hi - sys_hi_clamped
    sys_wide = np.concatenate(
        [
            np.zeros(pad_left, dtype=np.float32),
            sys_f32[sys_lo_clamped:sys_hi_clamped],
            np.zeros(pad_right, dtype=np.float32),
        ]
    )
    pre_pad = search_samples

    if len(sys_wide) < len(mic_win):
        return 0, 0.0

    lag, peak = echo_guard.estimate_delay(
        mic_win, sys_wide, pre_pad, search_samples
    )
    # echo_guard.estimate_delay returns the offset INTO sys_wide where
    # mic best aligns. Negative offsets mean sys leads mic — i.e. the
    # mic captures echo of audio that played out the speaker earlier.
    # Flip the sign so the rest of this module (and the report) speaks
    # "echo travel time": positive lag = echo arrives this many samples
    # AFTER speaker-out. Matches the convention used in _align_reference.
    return -lag, peak
