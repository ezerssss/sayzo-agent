"""Unit tests for sayzo_agent.echo_guard.

Pure synthetic fixtures. Uses a mock speech detector (no Silero) so tests
are fast and deterministic. The load-bearing invariant is the
bias-toward-keeping-user-speech property: the never-drop-user fuzz tests
must stay green before shipping.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from sayzo_agent.config import EchoGuardConfig
from sayzo_agent.echo_guard import (
    EchoGuardReport,
    _merge_spans,
    _subtract_spans,
    classify_buffers,
    classify_mic_segment,
    zero_out_echo_regions,
)
from sayzo_agent.models import SessionBuffers, SpeechSegment


SR = 16000


# --------------------------------------------------------------------------
# Synthetic signal helpers
# --------------------------------------------------------------------------


def _synth_voice(duration_s: float, seed: int = 0, amp: float = 0.3,
                 f0: float = 150.0) -> np.ndarray:
    """Fake voice: a harmonic stack with randomized per-harmonic phases and
    amplitudes. Covers the telephony speech band and has enough complexity
    for coherence to behave like on real speech.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    sig = np.zeros(n, dtype=np.float32)
    k = 1
    while True:
        freq = f0 * k
        if freq > 4000:
            break
        a = rng.uniform(0.3, 1.0) / k
        phase = rng.uniform(0, 2 * np.pi)
        sig += (a * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)
        k += 1
    # Add low-level noise so coherence measures are stable
    sig += (rng.normal(0, 0.02, size=n)).astype(np.float32)
    peak = float(np.max(np.abs(sig)))
    if peak > 0:
        sig = sig / peak * amp
    return sig.astype(np.float32)


def _make_echo(
    sys: np.ndarray,
    attenuation: float = 0.1,
    delay_samples: int = 0,
    eq_band: tuple[float, float] | None = None,
    reverb_tail_ms: float = 0.0,
) -> np.ndarray:
    """Simulate sys played through speakers + picked up by mic: attenuation,
    optional delay, optional speaker-EQ bandpass, optional reverb tail.
    """
    echo = np.zeros_like(sys, dtype=np.float32)
    shifted = np.zeros_like(sys, dtype=np.float32)
    if delay_samples > 0:
        shifted[delay_samples:] = sys[:-delay_samples]
    elif delay_samples < 0:
        shifted[:delay_samples] = sys[-delay_samples:]
    else:
        shifted = sys.copy()
    echo = shifted * attenuation

    if eq_band is not None:
        lo, hi = eq_band
        sos = butter(4, [lo / (SR / 2), hi / (SR / 2)],
                     btype="bandpass", output="sos")
        echo = sosfilt(sos, echo).astype(np.float32)

    if reverb_tail_ms > 0:
        n_tail = int(reverb_tail_ms / 1000.0 * SR)
        alpha = 0.5 ** (1.0 / max(1, n_tail // 4))
        kernel = (alpha ** np.arange(n_tail)).astype(np.float32)
        kernel[0] = 1.0
        kernel /= float(np.sum(kernel))
        echo = np.convolve(echo, kernel, mode="full")[:len(sys)].astype(np.float32)

    return echo.astype(np.float32)


def _f32_to_buf(arr: np.ndarray) -> bytearray:
    clipped = np.clip(arr, -1.0, 1.0)
    return bytearray((clipped * 32767.0).astype(np.int16).tobytes())


def _make_buffers(
    mic: np.ndarray,
    sys: np.ndarray,
    segments: list[tuple[float, float]],
) -> SessionBuffers:
    """Build a SessionBuffers with the given mic/sys PCM and mic segments.
    sys_segments is left empty — the echo guard only reads sys PCM directly.
    """
    buffers = SessionBuffers()
    buffers.mic_pcm = _f32_to_buf(mic)
    buffers.sys_pcm = _f32_to_buf(sys)
    buffers.mic_segments = [
        SpeechSegment(source="mic", start_ts=s, end_ts=e) for s, e in segments
    ]
    return buffers


def _mock_speech_detector(pcm: np.ndarray) -> float:
    """Energy-based proxy for Silero: if the residual has meaningful RMS
    above a low noise floor, call it speech.

    Threshold (0.003 ≈ -50 dBFS) is tuned to mirror Silero's real behavior
    on our synthetic "voice" signals: Silero is sensitive to speech-like
    spectral structure even at low levels, so the "user speaking softly
    under loud echo" case — where the residual has user content at
    RMS ~0.005 — needs to register as speech. Pure-echo residuals, which
    contain only numerical subtraction noise at RMS <0.001, stay below.
    """
    if pcm is None or pcm.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
    return 0.85 if rms > 0.003 else 0.05


# --------------------------------------------------------------------------
# Core classification: echo cases (expect drop)
# --------------------------------------------------------------------------


def _default_cfg(**overrides) -> EchoGuardConfig:
    base = dict(subdivide_long_segments_secs=0.0)  # disable subdivision for focused tests
    base.update(overrides)
    return EchoGuardConfig(**base)


def test_pure_echo_zero_delay_is_dropped():
    voice = _synth_voice(2.0, seed=1)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic = np.concatenate([pre, _make_echo(voice, attenuation=0.15), pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)

    assert report.segments_dropped == 1
    assert report.segments_kept == 0
    assert len(buffers.mic_echo_segments) == 1
    assert buffers.mic_segments == []


def test_pure_echo_with_8ms_delay_is_dropped():
    voice = _synth_voice(2.0, seed=2)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic = np.concatenate([pre, _make_echo(voice, attenuation=0.1,
                                          delay_samples=int(0.008 * SR)), pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 1


def test_pure_echo_with_40ms_delay_is_dropped():
    voice = _synth_voice(2.0, seed=3)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic = np.concatenate([pre, _make_echo(voice, attenuation=0.1,
                                          delay_samples=int(0.040 * SR)), pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 1


def test_pure_echo_with_cheap_speaker_eq_is_dropped():
    voice = _synth_voice(2.0, seed=4)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic_echo = _make_echo(voice, attenuation=0.18,
                          delay_samples=int(0.005 * SR),
                          eq_band=(200.0, 6000.0))
    mic = np.concatenate([pre, mic_echo, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 1


def test_pure_echo_with_short_reverb_tail_is_dropped():
    """Short reverb tail within the FFT window. Uses a sparse IR that
    preserves the direct path (not a smoothed average), matching how real
    room reverb actually sounds."""
    voice = _synth_voice(2.0, seed=5, amp=0.5)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    # Sparse IR: direct path + a couple of discrete reflections within 20 ms.
    ir = np.zeros(int(0.020 * SR), dtype=np.float32)
    ir[0] = 1.0
    ir[int(0.006 * SR)] = 0.45
    ir[int(0.012 * SR)] = 0.20
    ir[int(0.018 * SR)] = 0.08
    direct = _make_echo(voice, attenuation=0.18,
                        delay_samples=int(0.006 * SR))
    mic_echo = np.convolve(direct, ir, mode="full")[:len(voice)].astype(np.float32)
    mic = np.concatenate([pre, mic_echo, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 1


# --------------------------------------------------------------------------
# Core classification: keep cases (expect keep — bias toward user)
# --------------------------------------------------------------------------


def test_user_alone_sys_silent_is_kept():
    user = _synth_voice(2.0, seed=10, f0=170.0)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.zeros(len(pre) + len(user) + len(pre), dtype=np.float32)
    mic = np.concatenate([pre, user, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0
    assert report.segments_kept == 1
    assert buffers.mic_echo_segments == []


def test_user_alone_sys_has_different_content_is_kept():
    user = _synth_voice(2.0, seed=11, f0=170.0)
    other = _synth_voice(2.0, seed=12, f0=110.0)  # different "voice"
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, other * 0.5, pre])
    mic = np.concatenate([pre, user, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0
    assert report.segments_kept == 1


def test_user_over_loud_different_sys_is_kept():
    """Double-talk with unrelated sys content: mic has user + unrelated-echo;
    coherence stays low because sys has different content than user."""
    user = _synth_voice(2.0, seed=13, f0=170.0)
    other = _synth_voice(2.0, seed=14, f0=110.0)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, other, pre])
    mic_mix = user + _make_echo(other, attenuation=0.25,
                                delay_samples=int(0.006 * SR))
    mic = np.concatenate([pre, mic_mix, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0


def test_loud_echo_over_soft_user_is_kept():
    """Load-bearing laptop test: speakers are loud, user is quiet. Pure ERLE
    would call this echo (residual tiny vs. mic total). The residual-speech
    check correctly sees user content in the residual and keeps."""
    voice = _synth_voice(2.0, seed=15)
    user = _synth_voice(2.0, seed=16, f0=220.0) * 0.08  # very soft user
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic_mix = _make_echo(voice, attenuation=0.35,
                         delay_samples=int(0.006 * SR)) + user
    mic = np.concatenate([pre, mic_mix, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0, (
        f"Loud-echo-soft-user dropped: coh={report.per_segment[0].coherence:.2f} "
        f"resid_p={report.per_segment[0].residual_speech_prob:.2f} "
        f"reason={report.per_segment[0].reason}"
    )


def test_reiteration_with_different_voice_timbre_is_kept():
    """User repeats the same words as sys 800 ms later, but with a different
    voice (different f0). Coherence should stay low because the waveforms
    differ even though the content is semantically the same."""
    sys_phrase = _synth_voice(1.0, seed=17, f0=120.0, amp=0.4)
    user_reiteration = _synth_voice(1.0, seed=18, f0=210.0, amp=0.3)
    pre = np.zeros(SR, dtype=np.float32)
    gap = np.zeros(int(0.8 * SR), dtype=np.float32)  # 800 ms pause
    sys = np.concatenate([pre, sys_phrase, gap, np.zeros(len(user_reiteration),
                                                          dtype=np.float32), pre])
    mic = np.concatenate([pre, np.zeros(len(sys_phrase), dtype=np.float32),
                          gap, user_reiteration, pre])

    # mic segment: the user's reiteration span
    seg_start = 1.0 + len(sys_phrase) / SR + 0.8
    seg_end = seg_start + len(user_reiteration) / SR
    buffers = _make_buffers(mic, sys, [(seg_start, seg_end)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0, (
        "Reiteration must be kept: coaching signal depends on it"
    )


# --------------------------------------------------------------------------
# Subdivision: long segments with interior echo get split
# --------------------------------------------------------------------------


def test_subdivision_keeps_user_half_drops_echo_half():
    """Silero merged an echo-first + user-second span into one segment.
    Subdivision at 1-second granularity should drop the echo half and keep
    the user half."""
    voice = _synth_voice(2.5, seed=20)
    user = _synth_voice(2.5, seed=21, f0=220.0)

    pre = np.zeros(SR, dtype=np.float32)
    # First half (0-2.5s): mic = echo of sys_voice; sys = voice
    # Second half (2.5-5s): mic = user; sys = silent
    sys_half1 = voice
    sys_half2 = np.zeros(len(user), dtype=np.float32)
    mic_half1 = _make_echo(voice, attenuation=0.15,
                           delay_samples=int(0.006 * SR))
    mic_half2 = user

    sys = np.concatenate([pre, sys_half1, sys_half2, pre])
    mic = np.concatenate([pre, mic_half1, mic_half2, pre])

    # One merged mic segment covering the whole 5 seconds
    buffers = _make_buffers(mic, sys, [(1.0, 6.0)])

    cfg = EchoGuardConfig(subdivide_long_segments_secs=4.0,
                          subdivide_window_secs=1.0,
                          subdivide_hop_secs=1.0)
    report = classify_buffers(buffers, SR, cfg,
                              speech_detector=_mock_speech_detector)

    # At least part of the echo half should be dropped.
    assert report.seconds_dropped > 0.5
    # And at least part of the user half should survive.
    kept_durations = sum(s.end_ts - s.start_ts for s in buffers.mic_segments)
    assert kept_durations > 0.5


# --------------------------------------------------------------------------
# zero_out_echo_regions
# --------------------------------------------------------------------------


def test_zero_out_echo_regions_silences_interior_and_tapers_edges():
    duration_s = 2.0
    signal = (np.ones(int(duration_s * SR), dtype=np.float32) * 0.5).astype(np.float32)
    pcm_bytes = (signal * 32767).astype(np.int16).tobytes()

    out = zero_out_echo_regions(pcm_bytes, [(0.5, 1.5)], SR, taper_ms=5.0)
    out_arr = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0

    # Outside the span: untouched
    assert np.allclose(out_arr[: int(0.5 * SR)], 0.5, atol=1e-3)
    assert np.allclose(out_arr[int(1.5 * SR):], 0.5, atol=1e-3)

    # Interior: fully zero'd (except the taper edges)
    taper_samples = int(0.005 * SR)
    interior_start = int(0.5 * SR) + taper_samples
    interior_end = int(1.5 * SR) - taper_samples
    assert np.all(out_arr[interior_start:interior_end] == 0.0)

    # Leading taper: monotonically decreasing (cos²)
    leading = out_arr[int(0.5 * SR): int(0.5 * SR) + taper_samples]
    assert leading[0] > leading[-1]
    assert leading[0] > 0
    assert leading[-1] < 0.05

    # Trailing taper: monotonically increasing (cos²)
    trailing = out_arr[int(1.5 * SR) - taper_samples: int(1.5 * SR)]
    assert trailing[-1] > trailing[0]
    assert trailing[-1] > 0
    assert trailing[0] < 0.05

    # No NaN
    assert not np.any(np.isnan(out_arr))


def test_zero_out_echo_regions_no_spans_returns_copy():
    pcm = (np.sin(np.linspace(0, 6.28, 1000)) * 0.4).astype(np.float32)
    pcm_bytes = (pcm * 32767).astype(np.int16).tobytes()
    out = zero_out_echo_regions(pcm_bytes, [], SR)
    assert out == pcm_bytes


def test_zero_out_echo_regions_empty_pcm_returns_empty():
    assert zero_out_echo_regions(b"", [(0.0, 1.0)], SR) == b""


# --------------------------------------------------------------------------
# Helpers: _subtract_spans, _merge_spans
# --------------------------------------------------------------------------


def test_subtract_spans_handles_no_overlap():
    assert _subtract_spans((0.0, 10.0), []) == [(0.0, 10.0)]
    assert _subtract_spans((0.0, 10.0), [(20.0, 30.0)]) == [(0.0, 10.0)]


def test_subtract_spans_interior_hole():
    assert _subtract_spans((0.0, 10.0), [(3.0, 5.0)]) == [(0.0, 3.0), (5.0, 10.0)]


def test_subtract_spans_left_edge():
    assert _subtract_spans((0.0, 10.0), [(0.0, 3.0)]) == [(3.0, 10.0)]


def test_subtract_spans_right_edge():
    assert _subtract_spans((0.0, 10.0), [(7.0, 10.0)]) == [(0.0, 7.0)]


def test_subtract_spans_full_overlap_removes_segment():
    assert _subtract_spans((0.0, 10.0), [(0.0, 10.0)]) == []


def test_subtract_spans_multiple_holes():
    result = _subtract_spans((0.0, 10.0), [(2.0, 3.0), (5.0, 6.0)])
    assert result == [(0.0, 2.0), (3.0, 5.0), (6.0, 10.0)]


def test_merge_spans_merges_adjacent():
    assert _merge_spans([(0.0, 2.0), (2.0, 4.0), (5.0, 6.0)]) == [(0.0, 4.0), (5.0, 6.0)]


def test_merge_spans_merges_overlapping():
    assert _merge_spans([(0.0, 3.0), (2.0, 5.0)]) == [(0.0, 5.0)]


def test_merge_spans_empty():
    assert _merge_spans([]) == []


# --------------------------------------------------------------------------
# classify_buffers: disabled / empty
# --------------------------------------------------------------------------


def test_classify_buffers_disabled_is_noop():
    voice = _synth_voice(2.0, seed=30)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic = np.concatenate([pre, _make_echo(voice, attenuation=0.2), pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])
    original_segments = list(buffers.mic_segments)

    report = classify_buffers(buffers, SR,
                              EchoGuardConfig(enabled=False),
                              speech_detector=_mock_speech_detector)
    assert not report.enabled
    assert report.segments_dropped == 0
    assert buffers.mic_segments == original_segments
    assert buffers.mic_echo_segments == []


def test_classify_buffers_no_mic_segments_is_noop():
    sys = np.zeros(SR, dtype=np.float32)
    mic = np.zeros(SR, dtype=np.float32)
    buffers = _make_buffers(mic, sys, [])
    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_kept == 0
    assert report.segments_dropped == 0


# --------------------------------------------------------------------------
# Fuzz / property tests — these are the load-bearing invariants
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_fuzz_pure_echo_always_dropped(seed: int):
    rng = np.random.default_rng(seed)
    amp = float(rng.choice([0.05, 0.1, 0.3]))
    delay_ms = float(rng.choice([0.0, 10.0, 30.0, 60.0]))
    delay_samples = int(delay_ms / 1000.0 * SR)

    voice = _synth_voice(2.0, seed=seed + 100)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    mic = np.concatenate([pre, _make_echo(voice, attenuation=amp,
                                          delay_samples=delay_samples), pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 1, (
        f"seed={seed} amp={amp} delay={delay_ms}ms: expected drop, kept "
        f"(coh={report.per_segment[0].coherence:.2f} "
        f"resid_p={report.per_segment[0].residual_speech_prob:.2f})"
    )


@pytest.mark.parametrize("seed", range(10))
def test_fuzz_user_speech_never_dropped(seed: int):
    """The load-bearing invariant: user speech with arbitrary unrelated sys
    content must NEVER be classified as echo. This was the failure mode of
    the prior voiceprint enrollment approach and is strictly worse than
    leaving some echo in."""
    rng = np.random.default_rng(seed + 200)
    user_f0 = float(rng.uniform(100, 220))
    sys_f0 = float(rng.uniform(100, 220))
    sys_amp = float(rng.uniform(0.1, 0.5))

    user = _synth_voice(2.0, seed=seed + 300, f0=user_f0)
    other = _synth_voice(2.0, seed=seed + 400, f0=sys_f0)
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, other * sys_amp, pre])
    mic = np.concatenate([pre, user, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0, (
        f"seed={seed} user_f0={user_f0:.0f} sys_f0={sys_f0:.0f}: user speech "
        f"was wrongly dropped (coh={report.per_segment[0].coherence:.2f} "
        f"resid_p={report.per_segment[0].residual_speech_prob:.2f})"
    )


@pytest.mark.parametrize("snr_db", [-10, -5, 0])
@pytest.mark.parametrize("seed", range(3))
def test_fuzz_soft_user_under_loud_echo_always_kept(snr_db: float, seed: int):
    """Laptop scenario: speaker echo ≫ user voice. User's speech must still
    survive — this is the reason residual-speech-check replaced ERLE."""
    voice = _synth_voice(2.0, seed=seed + 500)
    user = _synth_voice(2.0, seed=seed + 600, f0=200.0)
    user_gain = 10.0 ** (snr_db / 20.0) * 0.15  # echo amp = 0.15
    pre = np.zeros(SR, dtype=np.float32)
    sys = np.concatenate([pre, voice, pre])
    echo = _make_echo(voice, attenuation=0.15, delay_samples=int(0.006 * SR))
    mic = np.concatenate([pre, echo + user * user_gain, pre])
    buffers = _make_buffers(mic, sys, [(1.0, 3.0)])

    report = classify_buffers(buffers, SR, _default_cfg(),
                              speech_detector=_mock_speech_detector)
    assert report.segments_dropped == 0, (
        f"seed={seed} snr={snr_db}dB: soft user under loud echo was dropped "
        f"(coh={report.per_segment[0].coherence:.2f} "
        f"resid_p={report.per_segment[0].residual_speech_prob:.2f})"
    )


# --------------------------------------------------------------------------
# classify_mic_segment direct API
# --------------------------------------------------------------------------


def test_classify_mic_segment_returns_sub_spans_on_subdivision():
    voice = _synth_voice(2.5, seed=40)
    user = _synth_voice(2.5, seed=41, f0=220.0)
    pre = np.zeros(SR, dtype=np.float32)
    sys_half1 = voice
    sys_half2 = np.zeros(len(user), dtype=np.float32)
    mic_half1 = _make_echo(voice, attenuation=0.15, delay_samples=int(0.006 * SR))
    mic_half2 = user

    sys = np.concatenate([pre, sys_half1, sys_half2, pre])
    mic = np.concatenate([pre, mic_half1, mic_half2, pre])

    mic_f32 = np.frombuffer(_f32_to_buf(mic), dtype=np.int16).astype(np.float32) / 32768.0
    sys_f32 = np.frombuffer(_f32_to_buf(sys), dtype=np.int16).astype(np.float32) / 32768.0

    seg = SpeechSegment(source="mic", start_ts=1.0, end_ts=6.0)
    cfg = EchoGuardConfig(subdivide_long_segments_secs=4.0,
                          subdivide_window_secs=1.0,
                          subdivide_hop_secs=1.0)
    result = classify_mic_segment(mic_f32, sys_f32, seg, SR, cfg,
                                  _mock_speech_detector)

    # Should have found echo in the first half but not the second half.
    assert len(result.echo_spans) >= 1
    # The echo spans should all fall in the first half of the original segment.
    for es, ee in result.echo_spans:
        assert es < 3.8, f"echo span {es}-{ee} leaked into user half"
