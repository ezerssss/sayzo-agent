"""Tests for SileroVAD's monotonic-timestamp contract.

The refactor in v2.18 changed ``feed(frame)`` → ``feed(frame, frame_mono_ts)``
and made emitted ``SpeechSegment`` carry monotonic-seconds ``start_ts`` /
``end_ts`` (instead of VAD-sample-relative seconds). The detector's
``on_segment`` then subtracts ``session_t0_mono`` to rebase.

These tests pin the timestamp math without depending on Silero correctly
classifying synthetic audio — we stub ``_model`` so each test can script
the exact sequence of voiced/unvoiced chunk decisions and assert that the
emitted segment's timestamps line up with the frames we fed in.
"""
from __future__ import annotations

import numpy as np

from sayzo_agent.models import SpeechSegment
from sayzo_agent.vad import SileroVAD


# ---- test infra -------------------------------------------------------


class _StubSileroModel:
    """Drop-in for ``silero_vad.load_silero_vad(onnx=True)`` output.

    Returns scripted probabilities in order. Anything past the script
    end returns 0.0 (unvoiced). Implements ``reset_states`` so VAD's
    ``reset()`` can call it without errors.
    """

    def __init__(self, probs: list[float]) -> None:
        self._probs = probs
        self.call_count = 0

    def __call__(self, chunk, sample_rate):  # noqa: ANN001
        import torch
        prob = self._probs[self.call_count] if self.call_count < len(self._probs) else 0.0
        self.call_count += 1
        return torch.tensor(prob)

    def reset_states(self) -> None:
        pass


def _make_vad_with_probs(probs: list[float], **kwargs) -> SileroVAD:
    """Build a SileroVAD whose model returns scripted probabilities."""
    v = SileroVAD("mic", **kwargs)
    v._model = _StubSileroModel(probs)
    return v


def _frame(n_samples: int) -> np.ndarray:
    """Synthetic float32 frame — content doesn't matter, the stub model
    decides voiced/unvoiced."""
    return np.zeros(n_samples, dtype=np.float32)


# 320 samples = 20 ms at 16 kHz; 512 samples = one Silero chunk.
FRAME_SAMPLES = 320
CHUNK_DURATION = SileroVAD.SILERO_CHUNK / SileroVAD.SAMPLE_RATE  # ~32 ms


# ---- tests ------------------------------------------------------------


def test_segment_start_mono_anchors_to_voice_onset():
    """First voiced chunk's start_ts == the monotonic time of that chunk's
    first sample. Hangover (10 chunks of 0.0 prob) closes the segment."""
    # Script: 1 unvoiced chunk, then 8 voiced (>= min_speech 200 ms),
    # then 12 unvoiced (>= hangover 300 ms).
    probs = [0.0] + [0.9] * 8 + [0.0] * 12
    v = _make_vad_with_probs(probs, hangover_ms=300)
    base_ts = 1000.0
    segments: list[SpeechSegment] = []
    frame_count = 0
    # Each frame is 20 ms; each Silero chunk is ~32 ms. Feed 35 frames
    # so we cover the script with margin.
    for i in range(35):
        ts = base_ts + i * 0.020
        segments.extend(v.feed(_frame(FRAME_SAMPLES), ts))

    assert len(segments) == 1, f"expected 1 segment, got {len(segments)}: {segments}"
    seg = segments[0]
    # Voice onset = 2nd chunk in our script (index 1 → starts at 1 * CHUNK_DURATION
    # into the stream, anchored on base_ts).
    expected_start = base_ts + CHUNK_DURATION
    assert abs(seg.start_ts - expected_start) < CHUNK_DURATION, (
        f"start_ts {seg.start_ts} not within one chunk of {expected_start}"
    )


def test_segment_end_mono_anchors_to_last_voiced_chunk():
    """Segment end_ts is the end of the LAST voiced chunk before hangover
    fires (not the end of the hangover window itself)."""
    # 1 unvoiced + 8 voiced + 12 unvoiced (>= hangover); 8 voiced ≥ min_speech.
    probs = [0.0] + [0.9] * 8 + [0.0] * 12
    v = _make_vad_with_probs(probs, hangover_ms=300)
    base_ts = 2000.0
    segments: list[SpeechSegment] = []
    for i in range(40):
        ts = base_ts + i * 0.020
        segments.extend(v.feed(_frame(FRAME_SAMPLES), ts))

    assert len(segments) == 1
    seg = segments[0]
    # Voice runs chunks 1..8 (8 voiced chunks). end_ts is the end of
    # chunk #8 = (1+8) * CHUNK_DURATION since base_ts.
    expected_end = base_ts + 9 * CHUNK_DURATION
    assert abs(seg.end_ts - expected_end) < CHUNK_DURATION, (
        f"end_ts {seg.end_ts} not within one chunk of {expected_end}"
    )


def test_flush_yields_in_progress_segment_with_mono_times():
    """A still-open segment at the moment of ``flush()`` emits with the
    monotonic times we anchored when feeding — the hangover never fires
    because we never get the unvoiced chunks."""
    # 1 unvoiced + 8 voiced (≥ min_speech 200 ms). No unvoiced after,
    # so the segment stays in-progress; flush emits it.
    probs = [0.0] + [0.9] * 8
    v = _make_vad_with_probs(probs, hangover_ms=300)
    base_ts = 3000.0
    # 9 chunks * 32 ms ≈ 288 ms. At 20 ms/frame → 15 frames suffice.
    yielded_from_feed: list[SpeechSegment] = []
    for i in range(15):
        ts = base_ts + i * 0.020
        yielded_from_feed.extend(v.feed(_frame(FRAME_SAMPLES), ts))
    assert yielded_from_feed == []  # mid-speech, no hangover yet

    flushed = list(v.flush())
    assert len(flushed) == 1, f"expected 1 segment from flush, got {flushed}"
    seg = flushed[0]
    # Voice runs chunks 1..8. start = 1 chunk in, end = 9 chunks in.
    expected_start = base_ts + CHUNK_DURATION
    expected_end = base_ts + 9 * CHUNK_DURATION
    assert abs(seg.start_ts - expected_start) < CHUNK_DURATION
    assert abs(seg.end_ts - expected_end) < CHUNK_DURATION
    # After flush, in-progress state is cleared.
    assert v._in_speech is False
    assert list(v.flush()) == []


def test_reset_then_feed_re_anchors_buf_start_mono():
    """Reset between two feeds with different timestamp bases — the second
    segment must anchor against the SECOND base, not carry over from the first."""
    v = _make_vad_with_probs([0.9] * 8 + [0.0] * 12)
    # Drain the first cycle (its segment is irrelevant — we only care
    # that state gets reset cleanly before the second cycle).
    base_one = 4000.0
    for i in range(30):
        list(v.feed(_frame(FRAME_SAMPLES), base_one + i * 0.020))

    v.reset()
    # Reinstall the stub with a fresh script (reset() doesn't reset the
    # stub's call_count — it's external state).
    v._model = _StubSileroModel([0.0] + [0.9] * 8 + [0.0] * 12)
    base_two = 9000.0  # 5 s gap from base_one — well outside any tolerance.
    segs: list[SpeechSegment] = []
    for i in range(40):
        segs.extend(v.feed(_frame(FRAME_SAMPLES), base_two + i * 0.020))

    assert len(segs) == 1
    expected_start = base_two + CHUNK_DURATION
    assert abs(segs[0].start_ts - expected_start) < CHUNK_DURATION
    # And NOT anchored to base_one (would be ~5000 s earlier).
    assert segs[0].start_ts > base_one + 100


def test_buf_carry_over_does_not_re_anchor_mid_chunk():
    """When ``feed()`` is called with leftover samples in _buf, the next
    chunk's start_ts must trace back to the FIRST frame that contributed
    to it (the anchor sample), not the latest frame's timestamp.

    Two 20 ms frames combined = 640 samples > 512 SILERO_CHUNK. The
    chunk consumes samples 0..511 (mostly frame 1 + 192 samples of
    frame 2). The chunk's anchor in monotonic time should be the
    timestamp of frame 1, not frame 2.
    """
    v = _make_vad_with_probs([0.9] * 5 + [0.0] * 12)
    base_ts = 7000.0
    frame1_ts = base_ts
    frame2_ts = base_ts + 0.020

    # Frame 1: 320 samples in, _buf has 320 samples, no chunk yet.
    out1 = list(v.feed(_frame(FRAME_SAMPLES), frame1_ts))
    assert out1 == []
    # _buf_start_mono was set to frame1_ts when buf was empty.
    assert v._buf_start_mono == frame1_ts

    # Frame 2: another 320 samples → 640 total → one chunk consumed.
    # That chunk's start should be frame1_ts (the anchor), advancing to
    # frame1_ts + CHUNK_DURATION afterwards.
    list(v.feed(_frame(FRAME_SAMPLES), frame2_ts))
    # In-progress speech segment should have started at frame1_ts —
    # NOT at frame2_ts.
    assert v._speech_start_mono is not None
    assert abs(v._speech_start_mono - frame1_ts) < 1e-6, (
        f"_speech_start_mono should anchor to frame1_ts ({frame1_ts}), "
        f"got {v._speech_start_mono}"
    )
    # _buf_start_mono should have advanced by exactly one chunk's duration.
    assert abs(v._buf_start_mono - (frame1_ts + CHUNK_DURATION)) < 1e-6
