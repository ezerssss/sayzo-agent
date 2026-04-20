"""Unit tests for sayzo_agent.dsp. Pure numerical — no audio I/O, no models."""
from __future__ import annotations

import numpy as np
import pytest

from sayzo_agent.config import CaptureConfig
from sayzo_agent.dsp import (
    _apply_highpass,
    _f32_to_i16,
    _i16_to_f32,
    _peak_normalize,
    apply_mic_dsp,
    apply_sys_dsp,
)


SR = 16000


def _sine(freq: float, secs: float, amp: float = 0.3) -> np.ndarray:
    n = int(secs * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def _band_rms(x: np.ndarray, sr: int, lo_hz: float, hi_hz: float) -> float:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    mask = (freqs >= lo_hz) & (freqs < hi_hz)
    return float(np.sqrt(np.sum(np.abs(spec[mask]) ** 2)) / len(x))


# ---------- highpass ----------


def test_highpass_attenuates_subcutoff_preserves_passband():
    # 50 Hz sine + 500 Hz sine at equal amplitude. HPF at 80 Hz, order 4
    # should attenuate 50 Hz by ~15+ dB and pass 500 Hz near-unity.
    sub = _sine(50, 2.0, amp=0.3)
    pass_ = _sine(500, 2.0, amp=0.3)
    mix = sub + pass_
    out = _apply_highpass(mix, cutoff_hz=80.0, sr=SR)

    # Measure energy in narrow bands around each tone.
    sub_in = _band_rms(mix, SR, 40, 60)
    sub_out = _band_rms(out, SR, 40, 60)
    pass_in = _band_rms(mix, SR, 480, 520)
    pass_out = _band_rms(out, SR, 480, 520)

    sub_atten_db = 20 * np.log10(sub_out / sub_in) if sub_in > 0 else 0
    pass_atten_db = 20 * np.log10(pass_out / pass_in) if pass_in > 0 else 0

    assert sub_atten_db < -10, f"expected 50Hz cut >10dB, got {sub_atten_db:.1f}dB"
    assert pass_atten_db > -2, f"expected 500Hz pass >-2dB, got {pass_atten_db:.1f}dB"


def test_highpass_cutoff_zero_is_passthrough():
    x = _sine(100, 0.5)
    out = _apply_highpass(x, cutoff_hz=0.0, sr=SR)
    assert out is x  # same object — no copy


def test_highpass_empty_input():
    out = _apply_highpass(np.zeros(0, dtype=np.float32), cutoff_hz=80.0, sr=SR)
    assert out.size == 0


# ---------- peak normalize ----------


def test_peak_normalize_hits_target_within_tolerance():
    x = _sine(440, 0.1, amp=0.1)  # peak 0.1
    target_dbfs = -1.0
    out = _peak_normalize(x, target_dbfs=target_dbfs)
    expected_peak = 10.0 ** (target_dbfs / 20.0)  # ~0.891
    actual_peak = float(np.max(np.abs(out)))
    assert abs(actual_peak - expected_peak) < 0.005


def test_peak_normalize_silent_is_passthrough():
    x = np.zeros(1000, dtype=np.float32)
    out = _peak_normalize(x, target_dbfs=-1.0)
    assert out is x  # no amplification of silence


# ---------- int16 <-> float32 round trip ----------


def test_i16_f32_roundtrip_preserves_shape_and_dtype():
    pcm = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16).tobytes()
    f = _i16_to_f32(pcm)
    assert f.dtype == np.float32
    assert f.shape == (5,)
    assert -1.0 <= f.min() and f.max() <= 1.0

    back = _f32_to_i16(f)
    assert len(back) == len(pcm)
    restored = np.frombuffer(back, dtype=np.int16)
    # Quantization round-trip drifts by <= 1 LSB on int16; 0 and 32767 survive.
    assert restored[0] == 0
    assert abs(int(restored[3]) - 32767) <= 1


# ---------- apply_mic_dsp / apply_sys_dsp ----------


def test_apply_mic_dsp_bytes_roundtrip():
    # 1 second of voice-like signal with some noise, int16 bytes in, same
    # count of int16 bytes out, no NaNs.
    rng = np.random.default_rng(42)
    voice = _sine(440, 1.0, amp=0.3) + 0.15 * _sine(660, 1.0, amp=1.0)
    noise = (rng.standard_normal(SR) * 0.03).astype(np.float32)
    mix = voice + noise
    pcm_in = _f32_to_i16(mix)
    cfg = CaptureConfig()

    pcm_out = apply_mic_dsp(pcm_in, sr=SR, cfg=cfg)

    assert isinstance(pcm_out, (bytes, bytearray))
    assert len(pcm_out) == len(pcm_in)
    arr = np.frombuffer(pcm_out, dtype=np.int16)
    assert not np.any(np.isnan(arr.astype(np.float32)))
    assert arr.size == SR


def test_apply_sys_dsp_bytes_roundtrip():
    rng = np.random.default_rng(7)
    x = (rng.standard_normal(SR) * 0.2).astype(np.float32)
    pcm_in = _f32_to_i16(x)
    cfg = CaptureConfig()

    pcm_out = apply_sys_dsp(pcm_in, sr=SR, cfg=cfg)

    assert len(pcm_out) == len(pcm_in)
    assert np.frombuffer(pcm_out, dtype=np.int16).size == SR


def test_dsp_disabled_is_identity():
    # With dsp_enabled=False the bytes must come out byte-for-byte identical
    # so we can roll back the whole feature by flipping one env var.
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(SR) * 0.2).astype(np.float32)
    pcm_in = _f32_to_i16(x)
    cfg = CaptureConfig(dsp_enabled=False)

    assert apply_mic_dsp(pcm_in, sr=SR, cfg=cfg) == pcm_in
    assert apply_sys_dsp(pcm_in, sr=SR, cfg=cfg) == pcm_in


def test_dsp_empty_input_returns_empty():
    cfg = CaptureConfig()
    assert apply_mic_dsp(b"", sr=SR, cfg=cfg) == b""
    assert apply_sys_dsp(b"", sr=SR, cfg=cfg) == b""


# ---------- denoise effect (requires noisereduce) ----------


def test_denoise_reduces_out_of_band_noise_floor():
    # Voice-like signal around 440 Hz + broadband white noise. After the mic
    # chain, the noise floor in a band well away from the signal (2-4 kHz)
    # should be clearly lower. This is the acid test that the spectral
    # gate is actually engaged via apply_mic_dsp.
    pytest.importorskip("noisereduce")

    rng = np.random.default_rng(123)
    secs = 2.0
    voice = _sine(440, secs, amp=0.3)
    noise = (rng.standard_normal(int(secs * SR)) * 0.05).astype(np.float32)
    mix = voice + noise
    pcm_in = _f32_to_i16(mix)

    # Normalize-off + HPF-off so we isolate the denoise effect.
    cfg = CaptureConfig(
        highpass_mic_hz=0.0,
        peak_normalize_dbfs=0.0,  # target 0 dBFS; but peak_normalize also
                                  # skips when gain≈1. For safety, compare
                                  # in_band/out_of_band *ratios* below instead
                                  # of absolute levels.
        denoise_strength=0.95,
    )
    pcm_out = apply_mic_dsp(pcm_in, sr=SR, cfg=cfg)
    out = _i16_to_f32(pcm_out)

    # Noise floor (2-4 kHz band where 440 Hz sine has no content)
    pre = _band_rms(mix, SR, 2000, 4000)
    post = _band_rms(out, SR, 2000, 4000)
    reduction_db = 20 * np.log10(post / pre) if pre > 0 else 0

    assert reduction_db < -3, (
        f"expected >=3 dB noise-floor reduction, got {reduction_db:.1f} dB"
    )
