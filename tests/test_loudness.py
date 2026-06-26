"""Unit tests for sayzo_agent.loudness — joint inter-channel loudness match.

Pure numerical; no audio I/O. The LUFS path needs pyloudnorm, so those tests
skip when it isn't installed. Signals must be > 400 ms (> 6400 samples @ 16 kHz)
or the BS.1770 meter has no gating block to work with.
"""
from __future__ import annotations

import numpy as np
import pytest

from sayzo_agent.config import CaptureConfig
from sayzo_agent.dsp import _f32_to_i16, apply_mic_dsp
from sayzo_agent.loudness import _UNMEASURABLE, match_loudness

SR = 16000


def _sine_pcm(freq: float, secs: float, amp: float) -> bytes:
    n = int(secs * SR)
    t = np.arange(n, dtype=np.float64) / SR
    return _f32_to_i16((amp * np.sin(2 * np.pi * freq * t)).astype(np.float32))


def _peak(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    return float(np.max(np.abs(np.frombuffer(pcm16, dtype=np.int16)))) / 32768.0


def _measure(pcm16: bytes) -> float:
    pyln = pytest.importorskip("pyloudnorm")
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    return float(pyln.Meter(SR).integrated_loudness(x))


# ---------- two-channel meet-in-the-middle ----------


def test_two_channels_end_matched():
    pytest.importorskip("pyloudnorm")
    mic = _sine_pcm(440, 1.0, amp=0.05)  # quiet
    sys = _sine_pcm(440, 1.0, amp=0.4)   # ~18 dB louder
    cfg = CaptureConfig()
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    assert rep.ran and not rep.mic_only
    diff = abs(_measure(mic_out) - _measure(sys_out))
    assert diff < 1.0, f"channels not matched: {diff:.2f} LU apart"


def test_floor_lifts_quiet_pair_so_louder_is_not_cut_below_its_level():
    """The v3.22 floor: for quiet captures the match target is lifted toward the
    floor instead of sitting at the bare midpoint, so the louder (far-side)
    channel is barely cut and the quieter (mic) is boosted up to meet it. This
    test FAILS under pure meet-in-the-middle (target == midpoint)."""
    pytest.importorskip("pyloudnorm")
    mic = _sine_pcm(440, 1.0, amp=0.03)   # quieter
    sys = _sine_pcm(440, 1.0, amp=0.06)   # ~6 dB louder, both well below the floor
    cfg = CaptureConfig()  # floor = loudness_target_lufs = -18
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    midpoint = (rep.mic_lufs + rep.sys_lufs) / 2.0
    gap = abs(rep.mic_lufs - rep.sys_lufs)
    assert rep.common_target_lufs > midpoint + 0.5  # floor lifted it above midpoint
    assert rep.sys_gain_db > -(gap / 2.0)           # louder cut LESS than meet-in-middle
    assert abs(_measure(mic_out) - _measure(sys_out)) < 1.0  # still matched


def test_equal_loudness_pair_unchanged():
    """Identical channels are already matched -> no cut/boost, byte-for-byte
    out. Guards against spuriously cutting toward a target."""
    pytest.importorskip("pyloudnorm")
    pcm = _sine_pcm(440, 1.0, amp=0.5)
    cfg = CaptureConfig()
    mic_out, sys_out, rep = match_loudness(pcm, bytes(pcm), SR, cfg)
    assert rep.ran
    assert abs(rep.mic_gain_db) < 0.01 and abs(rep.sys_gain_db) < 0.01
    assert mic_out == pcm and sys_out == pcm  # untouched


# ---------- solo / mic-only sessions (kept since v3.21.0) ----------


def test_mic_only_leaves_system_untouched():
    pytest.importorskip("pyloudnorm")
    mic = _sine_pcm(220, 1.0, amp=0.05)
    sys = bytes(len(mic))  # silent
    cfg = CaptureConfig()
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    assert rep.mic_only is True
    assert sys_out == sys  # silent channel byte-for-byte unchanged
    # lone channel boosted toward the solo target, bounded by max_boost
    assert 0.0 < rep.mic_gain_db <= cfg.loudness_max_boost_db + 1e-6
    assert _peak(mic_out) > _peak(mic)  # actually got louder


# ---------- guards: silent / too-short ----------


def test_both_too_short_is_guarded_noop():
    # LUFS path: < 400 ms is too short for the BS.1770 gating block, so both
    # channels read _UNMEASURABLE -> skip_reason="both_silent", returned
    # unchanged, no exception. (The RMS fallback has no block-size floor; this
    # guard is LUFS-specific.)
    mic = _sine_pcm(440, 0.1, amp=0.3)
    sys = _sine_pcm(440, 0.1, amp=0.3)
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, CaptureConfig())
    assert not rep.ran
    assert rep.skip_reason == "both_silent"
    assert mic_out == mic and sys_out == sys
    assert np.all(np.isfinite(np.frombuffer(mic_out, dtype=np.int16).astype(float)))


def test_one_short_one_normal_is_solo_not_crash():
    pytest.importorskip("pyloudnorm")
    mic = _sine_pcm(220, 1.0, amp=0.1)
    sys = _sine_pcm(440, 0.1, amp=0.3)  # too short -> unmeasurable
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, CaptureConfig())
    assert rep.mic_only is True
    assert sys_out == sys  # untouched
    assert len(mic_out) == len(mic)


def test_both_empty_is_guarded():
    mic_out, sys_out, rep = match_loudness(b"", b"", SR, CaptureConfig())
    assert rep.skip_reason == "empty_buffers"
    assert mic_out == b"" and sys_out == b""


# ---------- joint peak ceiling ----------


def test_joint_ceiling_prevents_clip_and_preserves_match():
    pytest.importorskip("pyloudnorm")
    mic = _sine_pcm(440, 1.0, amp=0.9)
    sys = _sine_pcm(440, 1.0, amp=0.99)
    cfg = CaptureConfig()
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    ceiling = 10.0 ** (cfg.loudness_peak_ceiling_dbfs / 20.0)
    assert max(_peak(mic_out), _peak(sys_out)) <= ceiling + 1e-3
    assert rep.joint_attenuation_db < 0.0  # ceiling engaged
    assert abs(_measure(mic_out) - _measure(sys_out)) < 1.0  # still matched


# ---------- disabled flag (rollback path) ----------


def test_disabled_is_identity():
    mic = _sine_pcm(440, 1.0, amp=0.05)
    sys = _sine_pcm(440, 1.0, amp=0.4)
    cfg = CaptureConfig(loudness_match_enabled=False)
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    assert rep.skip_reason == "disabled"
    assert mic_out == mic and sys_out == sys


def test_dsp_peaknorm_runs_only_when_loudness_match_off():
    """Guards the dsp.py rollback: with matching OFF, apply_mic_dsp must still
    peak-normalize; with matching ON it must NOT (the joint stage owns it)."""
    quiet = _sine_pcm(440, 0.5, amp=0.1)  # -20 dBFS peak
    off = CaptureConfig(
        loudness_match_enabled=False, denoise_enabled=False, highpass_mic_hz=0.0
    )
    on = CaptureConfig(
        loudness_match_enabled=True, denoise_enabled=False, highpass_mic_hz=0.0
    )
    out_off = apply_mic_dsp(quiet, SR, off)
    out_on = apply_mic_dsp(quiet, SR, on)
    # OFF -> peak-normalized up toward target (capped at +6 dB): ~0.1*1.995
    assert _peak(out_off) > 0.18
    # ON -> no peak-normalize; peak stays ~ input (modulo quantization)
    assert abs(_peak(out_on) - _peak(quiet)) < 0.01


# ---------- RMS fallback (no pyloudnorm needed) ----------


def test_rms_fallback_matches_channels():
    mic = _sine_pcm(440, 1.0, amp=0.05)
    sys = _sine_pcm(440, 1.0, amp=0.4)
    cfg = CaptureConfig(loudness_method="rms")
    mic_out, sys_out, rep = match_loudness(mic, sys, SR, cfg)
    assert rep.ran and rep.method == "rms" and not rep.fallback_used
    # gated-RMS of the matched outputs should be close
    def _grms(pcm16: bytes) -> float:
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64) / 32768.0
        return 20.0 * np.log10(np.sqrt(np.mean(x**2)) + 1e-12)
    assert abs(_grms(mic_out) - _grms(sys_out)) < 1.0


def test_metadata_is_json_safe():
    import json
    # unmeasurable channel -> -inf internally must serialize as null
    mic = _sine_pcm(220, 1.0, amp=0.1)
    sys = bytes(len(mic))
    _m, _s, rep = match_loudness(mic, sys, SR, CaptureConfig())
    meta = rep.as_metadata()
    json.dumps(meta)  # must not raise
    assert meta["sys_lufs"] is None  # silent channel -> null, not -inf
    assert _UNMEASURABLE == float("-inf")
