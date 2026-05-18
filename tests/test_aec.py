"""Tests for sayzo_agent.aec — WebRTC AEC3 pre-pass over (mic, sys).

These tests run the actual livekit APM (no mock) — the whole point of the
module is that we delegate echo cancellation to a battle-tested native
library, so a unit test that bypassed it would be testing nothing useful.

If livekit isn't installed in CI, `_get_apm` returns None and `cancel_echo`
falls into the ``livekit_unavailable`` skip path. Those tests guard
against that by importing ``livekit.rtc.apm`` at module level and skipping
the whole file when it's missing.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("livekit.rtc.apm", reason="livekit not installed")

from sayzo_agent.aec import AecReport, cancel_echo
from sayzo_agent.config import AecConfig


SR = 16000


def _aec_only_cfg(**overrides) -> AecConfig:
    """Config with AEC on but NS/HPF off — for tests that measure AEC
    behavior in isolation. The default ``AecConfig`` now turns NS3 + HPF
    on alongside AEC3, but NS3 over-attenuates the synthetic harmonic
    signals these tests use (it's calibrated for real speech). Separate
    tests below cover the NS3-on path with appropriate inputs."""
    base = dict(enabled=True, noise_suppression=False, high_pass_filter=False)
    base.update(overrides)
    return AecConfig(**base)


# --------------------------------------------------------------------------
# Synthetic signal helpers — non-periodic so the xcorr lag estimator works
# (sine waves have ambiguous lag; chirps and modulated voices do not).
# --------------------------------------------------------------------------


def _f32_to_i16_bytes(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def _i16_bytes_to_f32(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


def _voice_low(duration_s: float, seed: int = 0, amp: float = 0.3) -> np.ndarray:
    """User-voice-like signal living strictly in [200, 700] Hz.

    Spectrally disjoint from :func:`_noise_high` so the AEC behavior we
    measure is the cancellation of echo, not a side-effect of AEC's mask
    treating correlated harmonics as echo (the realistic in-meeting
    double-talk attenuation that's outside the scope of these unit tests).
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    # Three harmonics in [200, 600] Hz with randomized phases.
    sig = np.zeros(n, dtype=np.float32)
    for k, freq in enumerate((200.0, 400.0, 600.0)):
        amp_k = rng.uniform(0.5, 1.0) / (k + 1)
        phase = rng.uniform(0, 2 * np.pi)
        sig += (amp_k * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)
    peak = float(np.max(np.abs(sig)))
    if peak > 0:
        sig = sig / peak * amp
    return sig.astype(np.float32)


def _noise_high(duration_s: float, seed: int = 0, amp: float = 0.4) -> np.ndarray:
    """Bandpassed-noise system-audio reference in [1500, 3500] Hz.

    Mimics far-side speech / meeting audio sitting in the upper voice
    band. Spectrally disjoint from :func:`_voice_low` by design.
    """
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(seed)
    n = int(duration_s * SR)
    raw = rng.normal(0, 1, n).astype(np.float32)
    sos = butter(4, [1500.0 / (SR / 2), 3500.0 / (SR / 2)],
                 btype="bandpass", output="sos")
    filt = sosfilt(sos, raw).astype(np.float32)
    peak = float(np.max(np.abs(filt)))
    if peak > 0:
        filt = filt / peak * amp
    return filt


def _delay(x: np.ndarray, samples: int) -> np.ndarray:
    if samples <= 0:
        return x.copy()
    out = np.zeros_like(x)
    out[samples:] = x[:-samples]
    return out


# --------------------------------------------------------------------------
# Skip / passthrough paths
# --------------------------------------------------------------------------


def test_disabled_returns_input_unchanged():
    mic = _f32_to_i16_bytes(_voice_low(2.0, seed=1))
    sys = _f32_to_i16_bytes(_noise_high(2.0, seed=2))
    out, rep = cancel_echo(mic, sys, SR, AecConfig(enabled=False))
    assert out == mic
    assert not rep.ran
    assert rep.skip_reason == "disabled"


def test_empty_buffers_short_circuit():
    out, rep = cancel_echo(b"", b"", SR, AecConfig(enabled=True))
    assert out == b""
    assert rep.skip_reason == "empty_buffers"


def test_sys_silent_skips_aec():
    """No reference signal → no echo path → no work for AEC. Skip."""
    mic = _f32_to_i16_bytes(_voice_low(2.0, seed=3))
    sys = b"\x00" * len(mic)
    out, rep = cancel_echo(mic, sys, SR, AecConfig(enabled=True))
    assert out == mic
    assert rep.skip_reason == "sys_silent"


def test_unsupported_sample_rate():
    mic = (np.zeros(11025) + 1000).astype(np.int16).tobytes()
    sys = mic
    out, rep = cancel_echo(mic, sys, 11025, AecConfig(enabled=True))
    assert out == mic
    assert rep.skip_reason.startswith("unsupported_sr")


# --------------------------------------------------------------------------
# Core: echo suppression on a pure speaker-bleed scenario
# --------------------------------------------------------------------------


def test_pure_echo_suppressed_at_least_20db():
    """mic = 0.3 × delayed(sys). Pipeline must produce a mic with the echo
    largely subtracted out. We measure on the LATE portion of the session
    (after AEC3's ~1-2s convergence window).
    """
    sys = _noise_high(3.0, seed=10, amp=0.4)
    echo = 0.3 * _delay(sys, 40)

    mic_bytes = _f32_to_i16_bytes(echo)
    sys_bytes = _f32_to_i16_bytes(sys)

    out, rep = cancel_echo(mic_bytes, sys_bytes, SR, _aec_only_cfg())
    assert rep.ran
    assert rep.frames_processed > 0

    out_f32 = _i16_bytes_to_f32(out)
    # Skip the first 1.5s — AEC3 takes time to converge on the impulse
    # response. Real-world meeting captures are minutes long, so the
    # convergence window is negligible at session scale.
    late_in = echo[int(1.5 * SR):]
    late_out = out_f32[int(1.5 * SR):]

    rms_in = _rms(late_in)
    rms_out = _rms(late_out)
    assert rms_in > 0.01, "fixture has no echo to remove"
    suppression_db = 20 * np.log10(rms_in / max(rms_out, 1e-6))
    assert suppression_db >= 20.0, (
        f"expected >=20 dB suppression, got {suppression_db:.1f} dB "
        f"(rms_in={rms_in:.4f}, rms_out={rms_out:.4f})"
    )


def test_no_echo_preserves_user_speech():
    """Headphone scenario: mic has user voice only, sys has unrelated audio.
    AEC must not damage the mic — user-speech band energy is preserved.

    Note: we use a low-band user voice (200–600 Hz) vs a high-band sys
    reference (1500–3500 Hz) so AEC3's mask doesn't get confused by
    overlapping harmonics. In real meetings, user and far-side speech
    DO overlap spectrally and AEC3 will attenuate user voice slightly
    during double-talk — that's the realistic AEC trade-off, not a
    regression. This test verifies the no-correlation case.
    """
    mic_voice = _voice_low(3.0, seed=20, amp=0.3)
    sys_other = _noise_high(3.0, seed=21, amp=0.3)

    mic_bytes = _f32_to_i16_bytes(mic_voice)
    sys_bytes = _f32_to_i16_bytes(sys_other)

    out, rep = cancel_echo(mic_bytes, sys_bytes, SR, _aec_only_cfg())
    assert rep.ran

    out_f32 = _i16_bytes_to_f32(out)
    late_in = mic_voice[int(1.5 * SR):]
    late_out = out_f32[int(1.5 * SR):]

    rms_in = _rms(late_in)
    rms_out = _rms(late_out)
    attenuation_db = 20 * np.log10(rms_in / max(rms_out, 1e-6))
    # With disjoint spectra, AEC3 should leave the user voice essentially
    # untouched. <=3 dB attenuation is generous.
    assert attenuation_db <= 3.0, (
        f"AEC over-attenuated clean mic by {attenuation_db:.1f} dB "
        f"(expected <=3.0 with spectrally disjoint user/sys)"
    )


def test_double_talk_preserves_user_attenuates_echo():
    """User speaks WHILE far-side is playing. The user-speech band must
    survive; the sys-only band must drop substantially.

    Spectrally disjoint user/sys for the same reasons as above; this
    tests the core AEC mechanism, not the realistic-overlap regime.
    """
    user = _voice_low(3.0, seed=30, amp=0.3)
    sys = _noise_high(3.0, seed=31, amp=0.4)
    echo = 0.3 * _delay(sys, 40)
    mic = user + echo

    out, rep = cancel_echo(
        _f32_to_i16_bytes(mic), _f32_to_i16_bytes(sys), SR,
        _aec_only_cfg(),
    )
    assert rep.ran
    out_f32 = _i16_bytes_to_f32(out)

    user_late = user[int(1.5 * SR):]
    out_late = out_f32[int(1.5 * SR):]
    echo_late = echo[int(1.5 * SR):]

    from scipy.fft import rfft, rfftfreq
    freqs = rfftfreq(len(out_late), 1.0 / SR)
    out_spec = np.abs(rfft(out_late))
    user_spec = np.abs(rfft(user_late))
    echo_spec = np.abs(rfft(echo_late))

    user_band = (freqs > 200) & (freqs < 700)
    sys_band = (freqs > 1500) & (freqs < 3500)

    user_in_energy = float(np.mean(user_spec[user_band] ** 2))
    user_out_energy = float(np.mean(out_spec[user_band] ** 2))
    sys_in_energy = float(np.mean(echo_spec[sys_band] ** 2))
    sys_out_energy = float(np.mean(out_spec[sys_band] ** 2))

    user_preservation_db = 10 * np.log10(
        user_out_energy / max(user_in_energy, 1e-12)
    )
    sys_suppression_db = 10 * np.log10(
        sys_in_energy / max(sys_out_energy, 1e-12)
    )

    # User voice should be essentially preserved (disjoint spectrum from
    # sys means AEC has no reason to attenuate it).
    assert user_preservation_db >= -3.0, (
        f"user voice over-attenuated: {user_preservation_db:.1f} dB"
    )
    # Echo band should drop by at least 15 dB.
    assert sys_suppression_db >= 15.0, (
        f"echo not suppressed enough: only {sys_suppression_db:.1f} dB"
    )


def test_report_populated_with_timing_and_rms():
    """Smoke: AecReport fields make sense after a real run."""
    mic = _f32_to_i16_bytes(_voice_low(2.0, seed=40, amp=0.25))
    sys = _f32_to_i16_bytes(_noise_high(2.0, seed=41, amp=0.3))
    out, rep = cancel_echo(mic, sys, SR, _aec_only_cfg())
    assert isinstance(rep, AecReport)
    assert rep.enabled
    assert rep.ran
    assert rep.frames_processed == 200  # 2 s of 10 ms frames
    assert rep.duration_ms > 0
    assert rep.mic_rms_before > 0
    assert rep.mic_rms_after >= 0
    assert rep.sys_rms > 0
    assert len(out) == len(mic)


def test_partial_tail_frame_passes_through_unchanged():
    """If the session length isn't a multiple of 10 ms, the trailing
    partial frame is left as-is (APM can't process less-than-10ms blocks).
    """
    n_full = SR  # 1 s = 100 full 10 ms frames
    n_tail = 53  # < 160 samples = less than one 10 ms block
    mic_arr = _voice_low(1.0, seed=50, amp=0.3)
    sys_arr = _noise_high(1.0, seed=51, amp=0.3)
    mic_arr = np.concatenate([mic_arr, np.zeros(n_tail, dtype=np.float32) + 0.1])
    sys_arr = np.concatenate([sys_arr, np.zeros(n_tail, dtype=np.float32) + 0.1])

    out, rep = cancel_echo(
        _f32_to_i16_bytes(mic_arr), _f32_to_i16_bytes(sys_arr), SR,
        _aec_only_cfg(),
    )
    assert rep.ran
    assert rep.frames_processed == n_full // 160
    # Output length matches input length exactly (tail copied through).
    assert len(out) == len(mic_arr) * 2  # int16 = 2 bytes


# --------------------------------------------------------------------------
# Lag estimation
# --------------------------------------------------------------------------


def test_noise_suppression_reduces_stationary_background():
    """NS3 (enabled by default in v3.5.2+) should attenuate stationary
    background noise on the mic when it's on, leaving the AEC-only path
    untouched. Compares mic-out RMS with NS on vs NS off on identical
    inputs — NS on must produce a lower RMS.
    """
    rng = np.random.default_rng(70)
    n = SR * 3
    mic_noise = (rng.normal(0, 0.05, n).astype(np.float32))
    sys = _noise_high(3.0, seed=71, amp=0.3)
    mic_bytes = _f32_to_i16_bytes(mic_noise)
    sys_bytes = _f32_to_i16_bytes(sys)

    _, rep_ns_off = cancel_echo(
        mic_bytes, sys_bytes, SR,
        AecConfig(enabled=True, noise_suppression=False, high_pass_filter=False),
    )
    _, rep_ns_on = cancel_echo(
        mic_bytes, sys_bytes, SR,
        AecConfig(enabled=True, noise_suppression=True, high_pass_filter=False),
    )

    assert rep_ns_off.ran
    assert rep_ns_on.ran
    attenuation_db = 20 * np.log10(
        rep_ns_off.mic_rms_after / max(rep_ns_on.mic_rms_after, 1e-9)
    )
    # 3 dB floor: catches "flag wired to nothing" failures while leaving
    # headroom for NS3 version drift across livekit releases. WebRTC NS3
    # typically suppresses white noise by 6–12 dB.
    assert attenuation_db >= 3.0, (
        f"NS=on only attenuated by {attenuation_db:.1f} dB (expected >=3.0)"
    )


def test_lag_estimator_finds_non_zero_delay_on_speech_like_signal():
    """With non-periodic broadband content, the xcorr lag should be
    close to the truth and the peak high enough to trust.

    Uses _noise_high (bandpassed noise) — non-periodic, so the xcorr peak
    is unambiguous (sine waves would produce period-aliased peaks).
    """
    sys = _noise_high(3.0, seed=60, amp=0.4)
    true_delay = 80  # 5 ms — well within the typical 30–60 ms range
    echo = 0.3 * _delay(sys, true_delay)

    out, rep = cancel_echo(
        _f32_to_i16_bytes(echo), _f32_to_i16_bytes(sys), SR,
        _aec_only_cfg(),
    )
    assert rep.ran
    assert abs(rep.lag_samples - true_delay) <= 4, (
        f"expected lag near {true_delay}, got {rep.lag_samples} "
        f"(peak={rep.lag_xcorr_peak:.3f})"
    )
    assert rep.lag_xcorr_peak >= 0.20
