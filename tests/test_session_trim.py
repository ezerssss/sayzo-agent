"""Unit tests for sayzo_agent.session_trim.apply_session_trim.

Pure — no audio I/O, no model loading. Exercises the slice + mic-only echo
zeroing model that v3.7 introduced.
"""
from __future__ import annotations

import numpy as np

from sayzo_agent.models import SpeechSegment
from sayzo_agent.session_trim import apply_session_trim


SR = 16000


def _pcm(seconds: float, value: int = 1000) -> bytes:
    """Constant-value int16 mono PCM of the requested duration."""
    return np.full(int(seconds * SR), value, dtype=np.int16).tobytes()


def test_slice_keeps_padded_region_both_channels():
    mic = _pcm(10.0, value=1000)
    sys = _pcm(10.0, value=2000)
    mic_segs = [SpeechSegment("mic", 4.0, 5.0)]
    sys_segs = [SpeechSegment("system", 6.0, 7.0)]
    mic_out, sys_out, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, [], pad_secs=0.5, sample_rate=SR
    )
    # Window: [first=4.0 - 0.5, last=7.0 + 0.5] = [3.5, 7.5]
    expected_samples = int(4.0 * SR)
    assert len(mic_out) == expected_samples * 2
    assert len(sys_out) == expected_samples * 2
    assert rep.kept_secs == 4.0
    assert rep.start_offset_secs == 3.5
    assert rep.end_offset_secs == 2.5
    # Mid silence between segments was kept as recorded audio — every sample
    # in the sliced range is the original non-zero PCM value.
    assert np.all(np.frombuffer(mic_out, dtype=np.int16) == 1000)
    assert np.all(np.frombuffer(sys_out, dtype=np.int16) == 2000)


def test_thinking_pause_preserved_as_recorded_audio():
    """Regression guard for the v3.7 behavior change.

    5 s speech, 8 s silent gap, 5 s speech — the gap was previously zeroed
    (because gap > final_audio_merge_gap_secs). Now it must survive
    byte-for-byte from the input PCM (still non-zero recorded audio).
    """
    mic = _pcm(20.0, value=1234)
    sys = _pcm(20.0, value=4321)
    mic_segs = [
        SpeechSegment("mic", 1.0, 6.0),
        SpeechSegment("mic", 14.0, 19.0),
    ]
    sys_segs = [
        SpeechSegment("system", 1.0, 6.0),
        SpeechSegment("system", 14.0, 19.0),
    ]
    mic_out, sys_out, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, [], pad_secs=0.5, sample_rate=SR
    )
    # Window: [0.5, 19.5] = 19.0s
    assert rep.kept_secs == 19.0
    arr_mic = np.frombuffer(mic_out, dtype=np.int16)
    arr_sys = np.frombuffer(sys_out, dtype=np.int16)
    # The full sliced range — including the 8 s thinking pause — is the
    # original non-zero PCM value. No zero-fill anywhere in the middle.
    assert np.all(arr_mic == 1234)
    assert np.all(arr_sys == 4321)


def test_slice_clamps_at_zero():
    mic = _pcm(5.0)
    sys = _pcm(5.0)
    # Speech starts at 0.1 s, pad 0.5 → start_sample would be -6400; clamp to 0.
    mic_segs = [SpeechSegment("mic", 0.1, 2.0)]
    sys_segs: list[SpeechSegment] = []
    _, _, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, [], pad_secs=0.5, sample_rate=SR
    )
    assert rep.start_offset_secs == 0.0


def test_slice_clamps_at_end():
    mic = _pcm(5.0)
    sys = _pcm(5.0)
    # Speech ends at 4.9 s, pad 0.5 → end_sample would be 86400 > total 80000.
    mic_segs = [SpeechSegment("mic", 3.0, 4.9)]
    sys_segs: list[SpeechSegment] = []
    mic_out, sys_out, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, [], pad_secs=0.5, sample_rate=SR
    )
    assert len(mic_out) == 5 * SR * 2 - int(2.5 * SR) * 2  # 2.5 -> 5.0
    # start_offset = max(0, 3.0 - 0.5) = 2.5; kept = 5.0 - 2.5 = 2.5
    assert rep.start_offset_secs == 2.5
    assert rep.kept_secs == 2.5
    assert rep.end_offset_secs == 0.0
    assert len(sys_out) == len(mic_out)


def test_slice_channel_lengths_aligned():
    """If DSP returns slightly different lengths (resampler rounding), the
    output MUST still be sample-aligned. AEC and server-side diarization
    depend on `mic_final[k]` and `sys_final[k]` representing the same wall-
    clock moment.
    """
    mic = _pcm(10.0)
    # sys one sample shorter than mic
    sys = _pcm(10.0)[: -2]
    mic_segs = [SpeechSegment("mic", 4.0, 5.0)]
    sys_segs = [SpeechSegment("system", 6.0, 7.0)]
    mic_out, sys_out, _ = apply_session_trim(
        mic, sys, mic_segs, sys_segs, [], pad_secs=0.5, sample_rate=SR
    )
    assert len(mic_out) == len(sys_out)


def test_mic_echo_zeroed_after_slice():
    mic = _pcm(10.0, value=1000)
    sys = _pcm(10.0, value=2000)
    mic_segs = [SpeechSegment("mic", 4.0, 8.0)]
    sys_segs: list[SpeechSegment] = []
    # Echo span in absolute session time. Re-indexed against start_offset=3.5,
    # this becomes [2.5, 3.0] of the output.
    echo = [SpeechSegment("mic", 6.0, 6.5)]
    mic_out, sys_out, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, echo, pad_secs=0.5, sample_rate=SR
    )
    arr_mic = np.frombuffer(mic_out, dtype=np.int16)
    arr_sys = np.frombuffer(sys_out, dtype=np.int16)
    # Output spans [3.5, 8.5] = 5.0 s. Echo at output-time [2.5, 3.0] zeroed.
    z_a, z_b = int(2.5 * SR), int(3.0 * SR)
    assert np.all(arr_mic[:z_a] == 1000)
    assert np.all(arr_mic[z_a:z_b] == 0)
    assert np.all(arr_mic[z_b:] == 1000)
    # Sys is untouched.
    assert np.all(arr_sys == 2000)
    assert abs(rep.echo_zeroed_secs - 0.5) < 1e-6


def test_mic_echo_clamps_partial_overlap():
    mic = _pcm(10.0, value=1000)
    sys = _pcm(10.0, value=2000)
    mic_segs = [SpeechSegment("mic", 4.0, 8.0)]
    sys_segs: list[SpeechSegment] = []
    # Echo span partially overlaps the slice start (3.5). [2.0, 4.5]
    # → re-indexed [-1.5, 1.0] → clamped to [0.0, 1.0] in output time.
    echo = [SpeechSegment("mic", 2.0, 4.5)]
    mic_out, _, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, echo, pad_secs=0.5, sample_rate=SR
    )
    arr = np.frombuffer(mic_out, dtype=np.int16)
    z_b = int(1.0 * SR)
    assert np.all(arr[:z_b] == 0)
    assert np.all(arr[z_b:] == 1000)
    assert abs(rep.echo_zeroed_secs - 1.0) < 1e-6


def test_mic_echo_outside_slice_dropped():
    mic = _pcm(10.0, value=1000)
    sys = _pcm(10.0, value=2000)
    mic_segs = [SpeechSegment("mic", 4.0, 5.0)]
    sys_segs: list[SpeechSegment] = []
    # Echo entirely before slice start (3.5) AND entirely after slice end (5.5).
    echo = [
        SpeechSegment("mic", 0.5, 2.0),
        SpeechSegment("mic", 7.0, 8.0),
    ]
    mic_out, _, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, echo, pad_secs=0.5, sample_rate=SR
    )
    arr = np.frombuffer(mic_out, dtype=np.int16)
    # No zeroing — both echo spans are outside the kept range.
    assert np.all(arr == 1000)
    assert rep.echo_zeroed_secs == 0.0


def test_no_segments_returns_empty_bytes():
    mic = _pcm(5.0)
    sys = _pcm(5.0)
    mic_out, sys_out, rep = apply_session_trim(
        mic, sys, [], [], [], pad_secs=0.5, sample_rate=SR
    )
    assert mic_out == b""
    assert sys_out == b""
    assert rep.kept_secs == 0.0
    assert rep.original_secs == 5.0


def test_trim_report_arithmetic():
    mic = _pcm(20.0)
    sys = _pcm(20.0)
    mic_segs = [SpeechSegment("mic", 5.0, 6.0)]
    sys_segs = [SpeechSegment("system", 9.0, 10.0)]
    echo = [
        SpeechSegment("mic", 5.5, 5.7),  # partial in slice
        SpeechSegment("mic", 18.0, 19.0),  # outside slice
    ]
    _, _, rep = apply_session_trim(
        mic, sys, mic_segs, sys_segs, echo, pad_secs=0.5, sample_rate=SR
    )
    # original = 20.0; slice [4.5, 10.5] → kept = 6.0; offsets sum to original.
    assert abs(rep.original_secs - 20.0) < 1e-6
    assert abs(rep.kept_secs - 6.0) < 1e-6
    assert abs(rep.start_offset_secs - 4.5) < 1e-6
    assert abs(rep.end_offset_secs - 9.5) < 1e-6
    assert (
        abs(
            rep.start_offset_secs + rep.kept_secs + rep.end_offset_secs
            - rep.original_secs
        )
        < 1e-6
    )
    # Only the [5.5, 5.7] echo is inside the kept range → 0.2 s zeroed.
    assert abs(rep.echo_zeroed_secs - 0.2) < 1e-6
