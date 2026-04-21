"""Echo guard: classify mic VAD segments as user speech or speaker-to-mic bleed.

Runs post-session on the aligned mic and system PCM buffers. Pure numpy/scipy
+ a Silero VAD instance for the residual-speech check. See
~/.claude/plans/we-have-a-big-twinkling-wilkes.md for the full design.

Two audio-level checks, both must fire to drop a segment:
  1. Welch-coherence in the speech band (weighted by mic PSD) is high.
  2. Residual = mic - Wiener-estimated-echo contains no speech.

Biased toward keeping user content — false negatives (echo slips through)
are strictly preferred to false positives (user speech dropped). The paid
server-side LLM is the second-pass safety net.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .config import EchoGuardConfig
from .models import SessionBuffers, SpeechSegment

log = logging.getLogger(__name__)


# A speech detector takes float32 PCM at 16 kHz and returns a probability in
# [0, 1] indicating how speech-like it is. Injected so tests can mock it
# without loading Silero.
SpeechDetector = Callable[[np.ndarray], float]


# ---- default Silero-based detector ----------------------------------------

_SILERO_MODEL = None


def _get_silero_model():
    """Lazy-load a dedicated Silero model for the residual-speech check.

    Separate instance from the live SileroVAD wrappers in `vad.py` because
    those are stateful and bound to the mic/system capture loops running on
    the asyncio thread; this one runs on the heavy-worker executor for
    post-session classification.
    """
    global _SILERO_MODEL
    if _SILERO_MODEL is None:
        from silero_vad import load_silero_vad
        _SILERO_MODEL = load_silero_vad(onnx=True)
    return _SILERO_MODEL


def default_speech_detector(pcm: np.ndarray) -> float:
    """Max Silero speech probability over 32-ms chunks of `pcm`.

    Max (not mean) biases toward keeping: any chunk showing strong speech
    content is evidence the user was talking somewhere in the window, and
    the pipeline's cost for keeping a wrongly-flagged segment is much lower
    than dropping a real user turn.
    """
    import torch

    if pcm is None or len(pcm) < 512:
        return 0.0
    model = _get_silero_model()
    try:
        model.reset_states()
    except Exception:
        pass

    probs: list[float] = []
    n_chunks = len(pcm) // 512
    for i in range(n_chunks):
        chunk = pcm[i * 512:(i + 1) * 512].astype(np.float32, copy=False)
        with torch.no_grad():
            p = float(model(torch.from_numpy(chunk), 16000).item())
        probs.append(p)
    if not probs:
        return 0.0
    return float(np.max(probs))


# ---- data types -----------------------------------------------------------


@dataclass
class _WindowResult:
    """Classification of a single time window (may be a full segment or a
    subdivision sub-window). ``is_echo`` is the only load-bearing field; the
    rest feed observability / debug dumps."""
    is_echo: bool
    reason: str
    mic_rms: float = 0.0
    sys_rms: float = 0.0
    lag_samples: int = 0
    xcorr_peak: float = 0.0
    coherence: float = 0.0
    residual_speech_prob: float = 0.0


@dataclass
class EchoSegmentResult:
    """Per-original-segment outcome."""
    original: SpeechSegment
    # Sub-spans (session-relative seconds) classified as echo. Empty = the
    # whole original segment is kept. One span covering the whole original
    # = whole segment is dropped. Multiple spans = subdivision found echo
    # regions interior to the segment.
    echo_spans: list[tuple[float, float]]
    # Representative scores (from the single classification or the median of
    # sub-window classifications) for logging.
    mic_rms: float = 0.0
    sys_rms: float = 0.0
    lag_samples: int = 0
    xcorr_peak: float = 0.0
    coherence: float = 0.0
    residual_speech_prob: float = 0.0
    # Reason string for the overall decision. "echo" / "keep:<sub_reason>" /
    # "subdivided:<n>/<total>".
    reason: str = ""


@dataclass
class EchoGuardReport:
    """Per-session summary passed to logs + record.json metadata."""
    enabled: bool
    segments_kept: int = 0
    segments_dropped: int = 0
    seconds_dropped: float = 0.0
    dropped_spans: list[tuple[float, float]] = field(default_factory=list)
    per_segment: list[EchoSegmentResult] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)


# ---- public API -----------------------------------------------------------


def classify_buffers(
    buffers: SessionBuffers,
    sample_rate: int,
    cfg: EchoGuardConfig,
    speech_detector: Optional[SpeechDetector] = None,
) -> EchoGuardReport:
    """Classify every mic VAD segment and mutate `buffers` in place.

    On exit:
      - `buffers.mic_segments` contains only user segments (echo spans
        removed, partial segments split around interior echo spans).
      - `buffers.mic_echo_segments` is populated with every dropped span.
      - The raw `buffers.mic_pcm` / `buffers.sys_pcm` are NOT modified; the
        caller is responsible for zeroing the echo regions before STT via
        `zero_out_echo_regions`.

    Returns an `EchoGuardReport` for logging and record.json metadata.
    """
    thresholds = {
        "coh_high": cfg.coh_high_threshold,
        "resid_speech_keep_prob": cfg.residual_speech_keep_prob,
        "min_xcorr_peak": cfg.min_xcorr_peak,
        "min_system_rms": cfg.min_system_rms,
    }

    if not cfg.enabled:
        return EchoGuardReport(enabled=False, segments_kept=len(buffers.mic_segments),
                               thresholds=thresholds)

    if speech_detector is None:
        speech_detector = default_speech_detector

    mic_pcm = _pcm16_to_float32(buffers.mic_pcm)
    sys_pcm = _pcm16_to_float32(buffers.sys_pcm)

    new_mic_segments: list[SpeechSegment] = []
    echo_segments: list[SpeechSegment] = []
    per_segment_results: list[EchoSegmentResult] = []

    for seg in buffers.mic_segments:
        result = classify_mic_segment(
            mic_pcm, sys_pcm, seg, sample_rate, cfg, speech_detector
        )
        per_segment_results.append(result)

        if not result.echo_spans:
            new_mic_segments.append(seg)
            continue

        # Split the original segment around the echo spans.
        keep_spans = _subtract_spans(
            (seg.start_ts, seg.end_ts), result.echo_spans
        )
        for ks, ke in keep_spans:
            # Drop sub-segments shorter than 100 ms — below this they're not
            # useful for gating or transcription.
            if ke - ks >= 0.1:
                new_mic_segments.append(
                    SpeechSegment(source="mic", start_ts=ks, end_ts=ke)
                )
        for es, ee in result.echo_spans:
            echo_segments.append(
                SpeechSegment(source="mic", start_ts=es, end_ts=ee)
            )

    buffers.mic_segments = new_mic_segments
    buffers.mic_echo_segments = echo_segments

    dropped_secs = sum(ee - es for es, ee in
                       ((s.start_ts, s.end_ts) for s in echo_segments))

    return EchoGuardReport(
        enabled=True,
        segments_kept=len(new_mic_segments),
        segments_dropped=len(echo_segments),
        seconds_dropped=float(dropped_secs),
        dropped_spans=[(s.start_ts, s.end_ts) for s in echo_segments],
        per_segment=per_segment_results,
        thresholds=thresholds,
    )


def classify_mic_segment(
    mic_pcm: np.ndarray,
    sys_pcm: np.ndarray,
    seg: SpeechSegment,
    sample_rate: int,
    cfg: EchoGuardConfig,
    speech_detector: SpeechDetector,
) -> EchoSegmentResult:
    """Classify a single mic VAD segment. If the segment is longer than
    `cfg.subdivide_long_segments_secs`, subdivide into sliding 1-s windows
    and report per-window echo spans; otherwise classify the whole segment."""
    duration = seg.end_ts - seg.start_ts
    should_subdivide = (
        cfg.subdivide_long_segments_secs > 0
        and duration > cfg.subdivide_long_segments_secs
    )

    if not should_subdivide:
        wr = _classify_window(
            mic_pcm, sys_pcm, seg.start_ts, seg.end_ts, sample_rate,
            cfg, speech_detector,
        )
        return EchoSegmentResult(
            original=seg,
            echo_spans=[(seg.start_ts, seg.end_ts)] if wr.is_echo else [],
            mic_rms=wr.mic_rms, sys_rms=wr.sys_rms,
            lag_samples=wr.lag_samples, xcorr_peak=wr.xcorr_peak,
            coherence=wr.coherence, residual_speech_prob=wr.residual_speech_prob,
            reason=("echo" if wr.is_echo else f"keep:{wr.reason}"),
        )

    # Subdivision: slide a window across the segment and classify each.
    win = cfg.subdivide_window_secs
    hop = cfg.subdivide_hop_secs if cfg.subdivide_hop_secs > 0 else win
    sub_windows: list[tuple[float, float, _WindowResult]] = []
    t = seg.start_ts
    # Include full-coverage windows
    while t + win <= seg.end_ts + 1e-9:
        wr = _classify_window(
            mic_pcm, sys_pcm, t, t + win, sample_rate, cfg, speech_detector,
        )
        sub_windows.append((t, t + win, wr))
        t += hop
    # Add a tail window covering [end-win, end] if the hop missed it.
    if sub_windows:
        last_end = sub_windows[-1][1]
        if seg.end_ts - last_end > hop * 0.5:
            tail_start = max(seg.start_ts, seg.end_ts - win)
            wr = _classify_window(
                mic_pcm, sys_pcm, tail_start, seg.end_ts, sample_rate,
                cfg, speech_detector,
            )
            sub_windows.append((tail_start, seg.end_ts, wr))
    else:
        # Segment shorter than one window — shouldn't happen when
        # should_subdivide is true, but be safe.
        wr = _classify_window(
            mic_pcm, sys_pcm, seg.start_ts, seg.end_ts, sample_rate,
            cfg, speech_detector,
        )
        sub_windows.append((seg.start_ts, seg.end_ts, wr))

    # Merge echo windows into contiguous echo spans.
    raw_echo = [(s, e) for (s, e, r) in sub_windows if r.is_echo]
    echo_spans = _merge_spans(raw_echo)

    # Clip echo spans to the segment bounds (windows may extend slightly
    # outside due to rounding).
    echo_spans = [
        (max(seg.start_ts, s), min(seg.end_ts, e))
        for s, e in echo_spans
        if min(seg.end_ts, e) > max(seg.start_ts, s)
    ]

    # Representative scores: medians across sub-windows.
    if sub_windows:
        coh_vals = [r.coherence for _, _, r in sub_windows]
        resid_vals = [r.residual_speech_prob for _, _, r in sub_windows]
        lag_vals = [r.lag_samples for _, _, r in sub_windows]
        xcorr_vals = [r.xcorr_peak for _, _, r in sub_windows]
        mic_rms_vals = [r.mic_rms for _, _, r in sub_windows]
        sys_rms_vals = [r.sys_rms for _, _, r in sub_windows]
    else:
        coh_vals = resid_vals = lag_vals = xcorr_vals = mic_rms_vals = sys_rms_vals = [0.0]

    n_echo = sum(1 for _, _, r in sub_windows if r.is_echo)
    reason = f"subdivided:{n_echo}/{len(sub_windows)}"

    return EchoSegmentResult(
        original=seg,
        echo_spans=echo_spans,
        mic_rms=float(np.median(mic_rms_vals)),
        sys_rms=float(np.median(sys_rms_vals)),
        lag_samples=int(np.median(lag_vals)),
        xcorr_peak=float(np.median(xcorr_vals)),
        coherence=float(np.median(coh_vals)),
        residual_speech_prob=float(np.median(resid_vals)),
        reason=reason,
    )


def zero_out_echo_regions(
    pcm16_bytes: bytes,
    echo_spans: list[tuple[float, float]],
    sample_rate: int,
    taper_ms: float = 5.0,
) -> bytes:
    """Return a copy of `pcm16_bytes` with `echo_spans` zero'd out.

    A cosine-squared fade is applied across the first and last `taper_ms` of
    each span, so Whisper's log-mel front end doesn't see a spectral cliff at
    echo → user transitions. Pure / unit-testable.
    """
    if not echo_spans or not pcm16_bytes:
        return bytes(pcm16_bytes)

    pcm = np.frombuffer(pcm16_bytes, dtype=np.int16).copy()
    n_samples = len(pcm)
    taper_samples = max(1, int(round(taper_ms / 1000.0 * sample_rate)))

    for start_s, end_s in echo_spans:
        i0 = max(0, int(round(start_s * sample_rate)))
        i1 = min(n_samples, int(round(end_s * sample_rate)))
        if i1 <= i0:
            continue

        span_len = i1 - i0
        t = min(taper_samples, span_len // 2)
        if t <= 0:
            pcm[i0:i1] = 0
            continue

        # ramp_down[k] goes 1 → 0 across t samples (cos² from 0 to π/2).
        ramp_down = (np.cos(np.linspace(0.0, np.pi / 2, t, dtype=np.float32))) ** 2
        # ramp_up[k] goes 0 → 1 across t samples.
        ramp_up = (np.cos(np.linspace(np.pi / 2, 0.0, t, dtype=np.float32))) ** 2

        pcm[i0:i0 + t] = (pcm[i0:i0 + t].astype(np.float32) * ramp_down).astype(np.int16)
        if i1 - t > i0 + t:
            pcm[i0 + t:i1 - t] = 0
        pcm[i1 - t:i1] = (pcm[i1 - t:i1].astype(np.float32) * ramp_up).astype(np.int16)

    return pcm.tobytes()


# ---- internals ------------------------------------------------------------


def _pcm16_to_float32(pcm_bytes) -> np.ndarray:
    if not pcm_bytes:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(bytes(pcm_bytes), dtype=np.int16).astype(np.float32)
    arr /= 32768.0
    return arr


def _rms(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))


def _subtract_spans(
    segment: tuple[float, float],
    echo_spans: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return `segment` minus the union of `echo_spans`."""
    result: list[tuple[float, float]] = [segment]
    for es, ee in echo_spans:
        new_result: list[tuple[float, float]] = []
        for s, e in result:
            ix_s = max(s, es)
            ix_e = min(e, ee)
            if ix_s >= ix_e:
                new_result.append((s, e))
            else:
                if s < ix_s:
                    new_result.append((s, ix_s))
                if ix_e < e:
                    new_result.append((ix_e, e))
        result = new_result
    return result


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping / adjacent spans."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[list[float]] = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _classify_window(
    mic_pcm: np.ndarray,
    sys_pcm: np.ndarray,
    t0: float,
    t1: float,
    sample_rate: int,
    cfg: EchoGuardConfig,
    speech_detector: SpeechDetector,
) -> _WindowResult:
    """Classify one time window [t0, t1] as echo or keep."""
    t0_s = max(0, int(round(t0 * sample_rate)))
    t1_s = min(len(mic_pcm), int(round(t1 * sample_rate)))
    n = t1_s - t0_s
    if n <= 0:
        return _WindowResult(is_echo=False, reason="empty")

    mic_slice = mic_pcm[t0_s:t1_s]
    if len(mic_slice) < n:
        mic_slice = np.concatenate(
            [mic_slice, np.zeros(n - len(mic_slice), dtype=np.float32)]
        )
    mic_rms = _rms(mic_slice)

    # sys RMS check uses [t0 - 500 ms, t1] to catch reverb tails just after
    # the system goes silent.
    sys_rms_pre = int(0.5 * sample_rate)
    sys_rms_start = max(0, t0_s - sys_rms_pre)
    sys_rms_slice = sys_pcm[sys_rms_start:t1_s]
    sys_rms = _rms(sys_rms_slice)

    if sys_rms < cfg.min_system_rms:
        return _WindowResult(is_echo=False, reason="sys_silent",
                             mic_rms=mic_rms, sys_rms=sys_rms)
    if mic_rms < cfg.min_mic_rms_for_test:
        return _WindowResult(is_echo=False, reason="mic_quiet",
                             mic_rms=mic_rms, sys_rms=sys_rms)

    # sys_wide covers [t0 - search, t1 + search] so we can slide the mic
    # within it to find the best alignment.
    search = int(round(cfg.xcorr_search_ms / 1000.0 * sample_rate))
    sys_start = max(0, t0_s - search)
    sys_end = min(len(sys_pcm), t1_s + search)
    sys_wide = sys_pcm[sys_start:sys_end]
    pre_pad = t0_s - sys_start

    lag, xcorr_peak = _estimate_delay(mic_slice, sys_wide, pre_pad, search)
    if xcorr_peak < cfg.min_xcorr_peak:
        return _WindowResult(is_echo=False, reason="xcorr_low",
                             mic_rms=mic_rms, sys_rms=sys_rms,
                             lag_samples=lag, xcorr_peak=xcorr_peak)

    d = pre_pad + lag
    if d < 0 or d + n > len(sys_wide):
        return _WindowResult(is_echo=False, reason="lag_oob",
                             mic_rms=mic_rms, sys_rms=sys_rms,
                             lag_samples=lag, xcorr_peak=xcorr_peak)
    sys_aligned = sys_wide[d:d + n]

    coh = _speech_band_coherence(mic_slice, sys_aligned, sample_rate, cfg)
    if coh < cfg.coh_high_threshold:
        return _WindowResult(is_echo=False, reason="coh_low",
                             mic_rms=mic_rms, sys_rms=sys_rms,
                             lag_samples=lag, xcorr_peak=xcorr_peak,
                             coherence=coh)

    residual = _wiener_residual(mic_slice, sys_aligned, sample_rate, cfg)
    resid_p = float(speech_detector(residual))
    if resid_p >= cfg.residual_speech_keep_prob:
        return _WindowResult(is_echo=False, reason="resid_has_speech",
                             mic_rms=mic_rms, sys_rms=sys_rms,
                             lag_samples=lag, xcorr_peak=xcorr_peak,
                             coherence=coh, residual_speech_prob=resid_p)

    return _WindowResult(
        is_echo=True, reason="echo",
        mic_rms=mic_rms, sys_rms=sys_rms,
        lag_samples=lag, xcorr_peak=xcorr_peak,
        coherence=coh, residual_speech_prob=resid_p,
    )


def _estimate_delay(
    mic: np.ndarray, sys_wide: np.ndarray, pre_pad: int, search: int,
) -> tuple[int, float]:
    """Find best alignment of `mic` within `sys_wide` restricted to
    lag ∈ [-search, +search] around `pre_pad`.

    Returns (best_lag, normalized_peak) where:
        mic ≈ c · sys_wide[pre_pad + best_lag : pre_pad + best_lag + len(mic)]
    `normalized_peak` is the cosine of the angle between the two vectors at
    the best lag (Pearson-like, range [-1, 1] after mean-centering).
    """
    from scipy.signal import correlate

    n = len(mic)
    if n == 0 or len(sys_wide) < n:
        return 0, 0.0
    mic_c = mic - float(np.mean(mic))
    sys_c = sys_wide - float(np.mean(sys_wide))
    mic_e = float(np.linalg.norm(mic_c))
    if mic_e < 1e-10:
        return 0, 0.0

    xcorr = correlate(sys_c, mic_c, mode="full")
    # For correlate(a, b, 'full'), xcorr[k] peaks when b aligns with
    # a[k - len(b) + 1 : k + 1]. So offset `d = k - len(b) + 1` into `a`.
    k_center = pre_pad + n - 1
    k_min = max(0, k_center - search)
    k_max = min(len(xcorr) - 1, k_center + search)
    if k_min > k_max:
        return 0, 0.0
    best_k = k_min + int(np.argmax(xcorr[k_min:k_max + 1]))
    best_lag = best_k - k_center

    d = pre_pad + best_lag
    if d < 0 or d + n > len(sys_wide):
        return 0, 0.0
    sys_win = sys_c[d:d + n]
    sys_e = float(np.linalg.norm(sys_win))
    if sys_e < 1e-10:
        return 0, 0.0
    norm_peak = float(xcorr[best_k]) / (mic_e * sys_e)
    # Clamp to [0, 1] — we only care about magnitude, and numerical noise
    # can push it slightly outside.
    norm_peak = float(np.clip(norm_peak, 0.0, 1.0))
    return best_lag, norm_peak


def _speech_band_coherence(
    mic: np.ndarray, sys: np.ndarray, sr: int, cfg: EchoGuardConfig,
) -> float:
    """Mean magnitude-squared coherence in [speech_band_lo, speech_band_hi],
    weighted by mic PSD so the metric reflects speech content, not HF hiss.
    """
    from scipy.signal import coherence, welch

    n = min(len(mic), len(sys))
    if n < 256:
        return 0.0
    mic = mic[:n]
    sys = sys[:n]
    nperseg = min(cfg.fft_window_samples, n)
    if nperseg < 256:
        return 0.0
    noverlap = nperseg // 2

    try:
        f, cxy = coherence(mic, sys, fs=sr, nperseg=nperseg, noverlap=noverlap)
        _, pmm = welch(mic, fs=sr, nperseg=nperseg, noverlap=noverlap)
    except Exception as e:
        log.debug("[echo_guard] coherence failed: %s", e)
        return 0.0

    band = (f >= cfg.speech_band_lo_hz) & (f <= cfg.speech_band_hi_hz)
    if not np.any(band):
        return 0.0
    weights = pmm[band]
    w_sum = float(np.sum(weights))
    if w_sum <= 1e-20:
        return 0.0
    return float(np.sum(cxy[band] * weights) / w_sum)


def _wiener_residual(
    mic: np.ndarray, sys: np.ndarray, sr: int, cfg: EchoGuardConfig,
) -> np.ndarray:
    """residual = mic - predicted, where predicted is the best linear
    estimate of mic from sys via an STFT-averaged Wiener filter.

    STFT averaging (as opposed to a one-shot full-signal FFT) prevents
    overfitting: a single-window Wiener would trivially set predicted == mic.
    Averaging per-frequency across all STFT windows yields a stable
    echo-path estimate that captures the room EQ + speaker coloration
    without swallowing user speech.
    """
    from scipy.signal import istft, stft

    n = min(len(mic), len(sys))
    if n < 256:
        return mic[:n].astype(np.float32, copy=True)

    mic = mic[:n].astype(np.float32, copy=False)
    sys = sys[:n].astype(np.float32, copy=False)

    nperseg = min(cfg.fft_window_samples, n)
    if nperseg < 256:
        return mic.copy()
    noverlap = nperseg // 2

    try:
        _, _, zm = stft(mic, fs=sr, nperseg=nperseg, noverlap=noverlap,
                        padded=True, boundary="zeros")
        _, _, zs = stft(sys, fs=sr, nperseg=nperseg, noverlap=noverlap,
                        padded=True, boundary="zeros")
    except Exception as e:
        log.debug("[echo_guard] stft failed: %s", e)
        return mic.copy()

    # Time-averaged Wiener: H(f) = <M S*> / <|S|^2>.
    pss = np.mean(np.abs(zs) ** 2, axis=1)
    pms = np.mean(zm * np.conj(zs), axis=1)
    # Light regularization to stabilize low-|S| bins without biasing the
    # estimate on strong bins. Too large → pure-echo residual grows and
    # the downstream VAD mistakes subtraction leakage for speech. Too
    # small → numerical noise amplification on near-silent bins.
    eps = (np.max(pss) if pss.size else 0.0) * 1e-4 + 1e-12
    h = pms / (pss + eps)

    # Cap |H| at a physically-plausible echo attenuation. A speaker-to-mic
    # echo path is always an attenuation (never amplification); typical
    # coupling is -10 to -20 dB = 0.1-0.3 magnitude, with 0.5 being a
    # generous upper bound. Capping here is the load-bearing defense for
    # double-talk: in bins where mic is dominated by the user (not sys),
    # an over-fit H would over-subtract the user's voice. The cap limits
    # the damage so the residual preserves speech when it's present.
    max_h = 0.5
    mag = np.abs(h)
    over = mag > max_h
    if np.any(over):
        h = np.where(over, h * (max_h / (mag + 1e-12)), h)

    zp = h[:, None] * zs
    try:
        _, predicted = istft(zp, fs=sr, nperseg=nperseg, noverlap=noverlap,
                             boundary=True)
    except Exception as e:
        log.debug("[echo_guard] istft failed: %s", e)
        return mic.copy()

    if len(predicted) >= n:
        predicted = predicted[:n]
    else:
        predicted = np.concatenate(
            [predicted, np.zeros(n - len(predicted), dtype=np.float32)]
        )

    residual = mic - predicted.astype(np.float32)
    return residual.astype(np.float32)
