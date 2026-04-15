"""Unit tests for the silence-bounded conversation detector and gate.

These tests use synthetic VAD events — no audio I/O, no model loading.
"""
from __future__ import annotations

import numpy as np

from sayzo_agent.config import ConversationConfig
from sayzo_agent.conversation import (
    ConversationDetector,
    SessionState,
    build_windowed_pcm,
    evaluate_user_turn_gate,
    merge_close_segments,
)
from sayzo_agent.models import SessionCloseReason, SpeechSegment


def _cfg(**overrides) -> ConversationConfig:
    base = dict(
        joint_silence_close_secs=10.0,
        max_session_secs=600.0,
        min_user_turn_secs=8.0,
        min_user_total_secs=15.0,
        min_user_turns_for_total=2,
        min_sys_voiced_secs=1.0,
    )
    base.update(overrides)
    return ConversationConfig(**base)


def test_merge_close_segments_preserves_short_gaps():
    """Two segments within the gap threshold are merged into one range."""
    segs = [
        SpeechSegment("mic", 0.0, 3.0),
        SpeechSegment("mic", 5.0, 8.0),  # 2s gap
    ]
    merged = merge_close_segments(segs, gap_secs=5.0)
    assert len(merged) == 1
    assert merged[0].start_ts == 0.0
    assert merged[0].end_ts == 8.0


def test_merge_close_segments_respects_long_gaps():
    """Segments further apart than the threshold are kept separate."""
    segs = [
        SpeechSegment("mic", 0.0, 3.0),
        SpeechSegment("mic", 15.0, 18.0),  # 12s gap
    ]
    merged = merge_close_segments(segs, gap_secs=5.0)
    assert len(merged) == 2


def test_merge_close_segments_ignores_source():
    """A mic segment followed by a nearby sys segment should merge — the
    helper is purely timestamp-based because build_windowed_pcm is too."""
    segs = [
        SpeechSegment("mic", 0.0, 3.0),
        SpeechSegment("system", 5.0, 8.0),  # 2s gap, different source
    ]
    merged = merge_close_segments(segs, gap_secs=5.0)
    assert len(merged) == 1
    assert merged[0].start_ts == 0.0
    assert merged[0].end_ts == 8.0


def test_merge_close_segments_disabled_when_gap_zero():
    """gap_secs <= 0 should just return the input sorted, no merging."""
    segs = [
        SpeechSegment("mic", 5.0, 8.0),
        SpeechSegment("mic", 0.0, 3.0),
    ]
    merged = merge_close_segments(segs, gap_secs=0.0)
    assert len(merged) == 2
    assert merged[0].start_ts == 0.0
    assert merged[1].start_ts == 5.0


def test_merge_close_segments_chains_multiple():
    """A, B, C each within gap of the previous all merge into one range."""
    segs = [
        SpeechSegment("mic", 0.0, 2.0),
        SpeechSegment("system", 3.0, 5.0),
        SpeechSegment("mic", 6.0, 9.0),
        SpeechSegment("mic", 20.0, 22.0),  # far away
    ]
    merged = merge_close_segments(segs, gap_secs=2.0)
    assert len(merged) == 2
    assert merged[0].start_ts == 0.0
    assert merged[0].end_ts == 9.0
    assert merged[1].start_ts == 20.0


def _frame(seconds: float, sr: int = 16000, amplitude: float = 0.1) -> np.ndarray:
    return np.full(int(seconds * sr), amplitude, dtype=np.float32)


def test_pre_buffer_backfills_opening_turn():
    """The opening segment's PCM must be backfilled from the pre-buffer.

    Simulates the real pipeline order: on_frame is called for every frame
    while IDLE (Silero buffers + waits for hangover), then on_segment fires
    retroactively with start_ts at the beginning of that buffered audio. The
    session's mic_pcm must contain the audio from seg.start_ts onward, not
    be empty.
    """
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    # Stream 5 seconds of "voiced" mic audio while still IDLE.
    frame_secs = 0.02
    total_secs = 5.0
    for i in range(int(total_secs / frame_secs)):
        d.on_frame("mic", _frame(frame_secs, sr), now=100.0 + i * frame_secs)
        d.on_frame("system", np.zeros(int(frame_secs * sr), dtype=np.float32), now=100.0 + i * frame_secs)
    assert d.state == SessionState.IDLE
    # VAD finally yields: a segment that started at t=0 on its clock and ran
    # to the end of what we buffered.
    d.on_segment(SpeechSegment("mic", 0.0, total_secs), now=100.0 + total_secs)
    assert d.state == SessionState.OPEN
    buffers = d._buffers
    assert buffers is not None
    # The full 5s should have been backfilled.
    expected_bytes = int(total_secs * sr) * 2
    assert abs(len(buffers.mic_pcm) - expected_bytes) <= 2 * 2  # allow 1-frame rounding
    # Segment got rebased: since trigger_start_ts was 0, offset is 0, so the
    # segment should still be (0, 5).
    assert buffers.mic_segments[0].start_ts == 0.0
    assert abs(buffers.mic_segments[0].end_ts - total_secs) < 1e-6


def test_pre_buffer_rebases_timeline_when_trigger_mid_stream():
    """If the triggering segment starts mid-pre-buffer (e.g. user was silent
    for a while before speaking), the session timeline rebases to the
    segment's start so mic_pcm[0] corresponds to seg.start_ts."""
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    # 3s of silence then 2s of "voiced" buffered while IDLE
    for i in range(int(5.0 / frame_secs)):
        d.on_frame("mic", _frame(frame_secs, sr), now=100.0 + i * frame_secs)
        d.on_frame("system", _frame(frame_secs, sr), now=100.0 + i * frame_secs)
    # Segment spans 3.0→5.0 on the VAD clock
    d.on_segment(SpeechSegment("mic", 3.0, 5.0), now=105.0)
    buffers = d._buffers
    assert buffers is not None
    # Backfill should be the last ~2s, not the full 5s.
    assert abs(len(buffers.mic_pcm) - int(2.0 * sr) * 2) <= 2 * 2
    # Timeline rebased: segment is now (0, 2)
    assert buffers.mic_segments[0].start_ts == 0.0
    assert abs(buffers.mic_segments[0].end_ts - 2.0) < 1e-6
    # Subsequent segment at VAD-clock 6.0 → session-clock 3.0
    d.on_segment(SpeechSegment("system", 6.0, 7.0), now=106.0)
    assert abs(buffers.sys_segments[-1].start_ts - 3.0) < 1e-6


def test_back_to_back_sessions_without_vad_reset():
    """Two sessions in a row with a monotonically-growing VAD clock.

    The VAD is never reset between sessions (app.py used to do this; we
    removed it in favor of the detector's rebasing logic). The second
    session's timeline must still start at 0 even though incoming segment
    timestamps are e.g. 3700+.
    """
    sr = 16000
    cfg = _cfg(joint_silence_close_secs=5.0)
    d = ConversationDetector(cfg, sample_rate=sr)

    # --- session 1: VAD clock 0..15 ---
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 10.0, 11.0), now=110.0)
    d.tick(120.0)  # past joint-silence threshold
    first = d.take_closed_session()
    assert first is not None
    assert first.mic_segments[0].start_ts == 0.0  # already started at 0

    # --- long idle gap, then session 2: VAD clock is now ~3700 ---
    # The VAD kept counting; the detector never reset it. A new segment
    # arrives at start_ts=3700.5 on the VAD clock.
    d.on_segment(SpeechSegment("mic", 3700.5, 3710.5), now=3800.0)
    assert d.state == SessionState.OPEN
    # Session 2's first segment should have been rebased to start at 0.
    assert d._buffers is not None
    assert d._buffers.mic_segments[0].start_ts == 0.0
    assert abs(d._buffers.mic_segments[0].end_ts - 10.0) < 1e-6
    # A subsequent segment at VAD-clock 3712 → session-clock 11.5
    d.on_segment(SpeechSegment("system", 3712.0, 3713.0), now=3802.0)
    assert abs(d._buffers.sys_segments[-1].start_ts - 11.5) < 1e-6
    assert abs(d._buffers.sys_segments[-1].end_ts - 12.5) < 1e-6


def test_pre_buffer_respects_max_size():
    """When the pre-buffer exceeds its cap, old samples are dropped and the
    offset counter is bumped so indexing stays correct."""
    sr = 16000
    d = ConversationDetector(_cfg(max_pre_buffer_secs=1.0), sample_rate=sr)
    frame_secs = 0.02
    # Push 3s while IDLE → buffer should cap at ~1s
    for i in range(int(3.0 / frame_secs)):
        d.on_frame("mic", _frame(frame_secs, sr), now=100.0 + i * frame_secs)
    assert len(d._pre_buffers["mic"]) <= int(1.0 * sr) * 2
    # _pre_start_sample should reflect the ~2s dropped off the front
    assert d._pre_start_sample["mic"] >= int(1.9 * sr)


def test_session_opens_on_first_segment():
    d = ConversationDetector(_cfg())
    assert d.state == SessionState.IDLE
    d.on_segment(SpeechSegment("mic", 0.0, 1.5), now=100.0)
    assert d.state == SessionState.OPEN


def test_session_stays_open_during_long_user_monologue():
    """Demo case: user talks for 10 minutes, system silent until the end."""
    d = ConversationDetector(_cfg())
    t = 100.0
    # Continuous mic speech with sub-threshold gaps
    for i in range(60):
        d.on_segment(SpeechSegment("mic", i * 9.0, i * 9.0 + 8.5), now=t + i * 9.0)
        d.tick(t + i * 9.0 + 0.1)
    assert d.state == SessionState.OPEN
    # Late "thanks" from other side
    d.on_segment(SpeechSegment("system", 540.5, 541.5), now=t + 540.5)
    assert d.state == SessionState.OPEN


def test_session_closes_on_joint_silence():
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    # Tick well after both went silent
    d.tick(120.0)
    assert d.state == SessionState.IDLE
    closed = d.take_closed_session()
    assert closed is not None
    assert closed.close_reason == SessionCloseReason.JOINT_SILENCE


def test_safety_cap_checkpoints_session():
    d = ConversationDetector(_cfg(max_session_secs=30.0))
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.tick(135.0)  # 35s after open → past cap
    closed = d.take_closed_session()
    assert closed is not None
    assert closed.close_reason == SessionCloseReason.SAFETY_CAP


def test_gate_passes_long_turn():
    cfg = _cfg()
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("mic", 0.0, 12.0), now=100.0)
    d.on_segment(SpeechSegment("system", 12.0, 14.0), now=112.0)
    d.tick(200.0)
    closed = d.take_closed_session()
    assert closed is not None
    result = evaluate_user_turn_gate(closed, cfg)
    assert result.passed
    assert "PASS" in result.reason


def test_gate_fails_only_filler_user_speech():
    """Long other-side, user only says 'yeah' — must be discarded."""
    cfg = _cfg()
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("system", 0.0, 240.0), now=100.0)
    d.on_segment(SpeechSegment("mic", 240.0, 240.8), now=340.0)  # 0.8s "yeah"
    d.on_segment(SpeechSegment("system", 240.8, 245.0), now=345.0)
    d.tick(400.0)
    closed = d.take_closed_session()
    assert closed is not None
    result = evaluate_user_turn_gate(closed, cfg)
    assert not result.passed
    assert "FAIL" in result.reason


def test_gate_passes_late_substantive_user_turn():
    """Other monologues → user 'mhm' → other → user gives 30s answer.
    The whole session must pass and be preserved (the late turn is gold).
    """
    cfg = _cfg()
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("system", 0.0, 240.0), now=100.0)
    d.on_segment(SpeechSegment("mic", 240.5, 241.5), now=341.5)  # "mhm"
    d.on_segment(SpeechSegment("system", 242.0, 360.0), now=460.0)
    d.on_segment(SpeechSegment("mic", 361.0, 391.0), now=491.0)  # 30s substantive
    d.on_segment(SpeechSegment("system", 392.0, 393.0), now=493.0)  # "thanks"
    d.tick(600.0)
    closed = d.take_closed_session()
    assert closed is not None
    result = evaluate_user_turn_gate(closed, cfg)
    assert result.passed
    assert result.mic_max_turn >= 8.0
    # Whole session preserved (we can see both early and late mic content)
    assert closed.mic_turn_count() == 2


def _make_pcm(seconds: float, sr: int = 16000, value: int = 1000) -> bytes:
    return (np.full(int(seconds * sr), value, dtype=np.int16)).tobytes()


def test_build_windowed_pcm_zeroes_outside_windows():
    sr = 16000
    pcm = _make_pcm(10.0, sr=sr, value=1000)  # 10s of constant non-zero
    seg = SpeechSegment("mic", 4.0, 5.0)  # one 1s segment
    out = build_windowed_pcm(pcm, [seg], pad_secs=0.5, sample_rate=sr)
    assert len(out) == len(pcm)
    arr = np.frombuffer(out, dtype=np.int16)
    # Window: [3.5s, 5.5s] → samples [56000, 88000]
    assert np.all(arr[: int(3.5 * sr)] == 0)
    assert np.all(arr[int(3.5 * sr) : int(5.5 * sr)] == 1000)
    assert np.all(arr[int(5.5 * sr) :] == 0)


def test_build_windowed_pcm_merges_overlapping_windows():
    sr = 16000
    pcm = _make_pcm(10.0, sr=sr, value=1000)
    segs = [SpeechSegment("mic", 3.0, 3.2), SpeechSegment("mic", 3.5, 3.7)]
    out = build_windowed_pcm(pcm, segs, pad_secs=1.0, sample_rate=sr)
    arr = np.frombuffer(out, dtype=np.int16)
    # Merged window: [2.0s, 4.7s]
    assert np.all(arr[: int(2.0 * sr)] == 0)
    assert np.all(arr[int(2.0 * sr) : int(4.7 * sr)] == 1000)
    assert np.all(arr[int(4.7 * sr) :] == 0)


def test_build_windowed_pcm_no_segments_returns_zeros():
    pcm = _make_pcm(2.0)
    out = build_windowed_pcm(pcm, [], pad_secs=1.0, sample_rate=16000)
    assert len(out) == len(pcm)
    assert np.all(np.frombuffer(out, dtype=np.int16) == 0)


def test_density_late_substantive_turn_is_above_threshold():
    """Sanity: the late-substantive-turn fixture stays on the full-STT path."""
    cfg = _cfg()
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("system", 0.0, 240.0), now=100.0)
    d.on_segment(SpeechSegment("mic", 240.5, 241.5), now=341.5)
    d.on_segment(SpeechSegment("system", 242.0, 360.0), now=460.0)
    d.on_segment(SpeechSegment("mic", 361.0, 391.0), now=491.0)
    d.on_segment(SpeechSegment("system", 392.0, 393.0), now=493.0)
    d.tick(600.0)
    closed = d.take_closed_session()
    assert closed is not None
    elapsed = closed.elapsed()
    density = closed.mic_total_voiced() / max(elapsed, 1e-6)
    assert density >= cfg.stt_full_density, (
        f"late-substantive-turn fixture must stay on full STT path; got density={density:.4f}"
    )


def test_density_passive_media_is_below_threshold():
    """A 60-min YouTube + one 10s comment should land in the windowed path."""
    cfg = _cfg(joint_silence_close_secs=120.0, max_session_secs=4000.0)
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("system", 0.0, 1800.0), now=100.0)
    d.on_segment(SpeechSegment("mic", 1800.0, 1810.0), now=1910.0)  # 10s comment
    d.on_segment(SpeechSegment("system", 1810.0, 3600.0), now=3700.0)
    d.tick(3900.0)
    closed = d.take_closed_session()
    assert closed is not None
    density = closed.mic_total_voiced() / max(closed.elapsed(), 1e-6)
    assert density < cfg.stt_full_density


def test_gate_fails_no_counterparty():
    """User talks to themselves — must be discarded."""
    cfg = _cfg()
    d = ConversationDetector(cfg)
    d.on_segment(SpeechSegment("mic", 0.0, 30.0), now=100.0)
    d.tick(200.0)
    closed = d.take_closed_session()
    assert closed is not None
    result = evaluate_user_turn_gate(closed, cfg)
    assert not result.passed
    assert "counterparty" in result.reason
