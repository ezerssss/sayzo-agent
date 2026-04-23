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
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
        d.on_frame("system", np.zeros(int(frame_secs * sr), dtype=np.float32), t, t)
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
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
        d.on_frame("system", _frame(frame_secs, sr), t, t)
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
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
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
    """Legacy path: no on_pending_close callback → silence closes immediately."""
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    # Tick well after both went silent
    d.tick(120.0)
    assert d.state == SessionState.IDLE
    closed = d.take_closed_session()
    assert closed is not None
    assert closed.close_reason == SessionCloseReason.JOINT_SILENCE


def test_pending_close_holds_session_when_callback_registered():
    """Armed model: on_pending_close takes responsibility for commit/revert."""
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_pending_close = lambda: None  # ArmController stand-in
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    d.tick(120.0)
    assert d.state == SessionState.PENDING_CLOSE
    # Nothing written yet — buffers are held.
    assert d.take_closed_session() is None


def test_pending_close_commit_produces_capture():
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_pending_close = lambda: None
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    d.tick(120.0)
    assert d.state == SessionState.PENDING_CLOSE
    d.commit_close(121.0, SessionCloseReason.JOINT_SILENCE)
    assert d.state == SessionState.IDLE
    closed = d.take_closed_session()
    assert closed is not None
    assert closed.close_reason == SessionCloseReason.JOINT_SILENCE


def test_pending_close_revert_keeps_session_open():
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_pending_close = lambda: None
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    d.tick(120.0)
    d.revert_close(121.0)
    assert d.state == SessionState.OPEN
    # Silence timer reset — tick shortly after must not re-enter PENDING_CLOSE
    d.tick(123.0)
    assert d.state == SessionState.OPEN
    # But prolonged silence eventually re-enters PENDING_CLOSE
    d.tick(127.0)
    assert d.state == SessionState.PENDING_CLOSE


def test_pending_close_auto_reverts_on_speech():
    """A VAD segment arriving during PENDING_CLOSE is ground truth that the
    meeting continued; the detector auto-reverts to OPEN."""
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_pending_close = lambda: None
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.on_segment(SpeechSegment("system", 9.0, 10.0), now=110.0)
    d.tick(120.0)
    assert d.state == SessionState.PENDING_CLOSE
    # User resumes talking
    d.on_segment(SpeechSegment("mic", 20.0, 22.0), now=122.0)
    assert d.state == SessionState.OPEN


def test_shutdown_during_pending_close_commits_not_discards():
    """force_close on PENDING_CLOSE must preserve the capture on the way out."""
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0))
    d.on_pending_close = lambda: None
    d.on_segment(SpeechSegment("mic", 0.0, 9.0), now=100.0)
    d.tick(120.0)
    assert d.state == SessionState.PENDING_CLOSE
    d.force_close(121.0)
    closed = d.take_closed_session()
    assert closed is not None
    assert closed.close_reason == SessionCloseReason.SHUTDOWN


def test_reset_source_epochs_clears_state():
    d = ConversationDetector(_cfg())
    d._source_epoch_mono["mic"] = 100.0
    d._source_frames_seen["mic"] = 50
    d._pre_buffers["mic"].extend(b"\x00" * 100)
    d._pre_start_sample["mic"] = 10
    d.reset_source_epochs()
    assert d._source_epoch_mono == {"mic": None, "system": None}
    assert d._source_frames_seen == {"mic": 0, "system": 0}
    assert len(d._pre_buffers["mic"]) == 0
    assert d._pre_start_sample["mic"] == 0


def test_reset_source_epochs_raises_when_not_idle():
    d = ConversationDetector(_cfg())
    d.on_segment(SpeechSegment("mic", 0.0, 1.0), now=100.0)
    assert d.state == SessionState.OPEN
    import pytest
    with pytest.raises(RuntimeError):
        d.reset_source_epochs()


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
    cfg = _cfg(joint_silence_close_secs=120.0)
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


# ----------------------------------------------------------------------
# Mono-clock invariant tests: verify that mic_pcm and sys_pcm are
# time-aligned regardless of per-source pre-buffer state, dropped frames,
# or tail-length mismatches. Regressing any of these would reintroduce
# the server-side speaker-tag-flip bug.
# ----------------------------------------------------------------------


def test_session_buffers_aligned_despite_unequal_pre_buffer_fill():
    """If mic and system have pre-buffered different amounts of audio
    before a session triggers, the session's mic_pcm and sys_pcm must
    still start at the same wall-clock moment (session_t0_mono).

    Regression guard for the server-side speaker-tag-flip bug: the fix
    uses capture_mono_ts to align both channels at session open. Here,
    we simulate mic starting 500 ms earlier than system and verify the
    resulting PCM buffers end at the same monotonic time.
    """
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    # Mic has 3 seconds of pre-buffer; system only has 2 seconds (started
    # later). Both share the same monotonic clock: mic at t=100.0, system
    # at t=100.5.
    t_mic_start = 100.0
    t_sys_start = 100.5
    for i in range(int(3.0 / frame_secs)):
        t = t_mic_start + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    for i in range(int(2.5 / frame_secs)):
        t = t_sys_start + i * frame_secs
        d.on_frame("system", _frame(frame_secs, sr), t, t)
    # Trigger: mic VAD fires a segment. In the never-reset VAD convention,
    # seg.start_ts is the sample-second index on mic's counter. Mic has
    # processed 3 seconds of samples, so a segment at the 2-second mark
    # (1s ago in wall-clock terms = t=102.0 monotonic) has start_ts=2.0.
    d.on_segment(SpeechSegment("mic", 2.0, 3.0), now=103.0)
    buffers = d._buffers
    assert buffers is not None
    # session_t0_mono = mic_epoch + 2.0 = 100.0 + 2.0 = 102.0.
    assert abs(buffers.session_t0_mono - 102.0) < 1e-3
    # mic_pcm should cover [102.0, 103.0] = 1 second.
    mic_dur = len(buffers.mic_pcm) / 2 / sr
    assert abs(mic_dur - 1.0) < 0.05, f"mic_pcm = {mic_dur:.3f}s (expected ~1.0s)"
    # sys_pcm should also cover [102.0, 103.0]. System's pre-buffer covered
    # [100.5, 103.0], so the slice from 102.0 onward = 1 second.
    sys_dur = len(buffers.sys_pcm) / 2 / sr
    assert abs(sys_dur - 1.0) < 0.05, f"sys_pcm = {sys_dur:.3f}s (expected ~1.0s)"
    # Cross-check: mic_pcm and sys_pcm have the same length (both end at
    # same mono time after the trigger segment's capture).
    assert abs(len(buffers.mic_pcm) - len(buffers.sys_pcm)) <= 2 * 2


def test_session_pads_front_when_source_started_after_session_t0():
    """If one source's pre-buffer starts AFTER session_t0_mono (e.g., that
    source's capture started later than the other), the detector zero-pads
    the front of that source's session PCM so both channels still align."""
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    # Mic has 3 seconds of pre-buffer starting at t=100.
    for i in range(int(3.0 / frame_secs)):
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    # System started 1 second later (t=101.0) and has only 2 seconds of pre.
    for i in range(int(2.0 / frame_secs)):
        t = 101.0 + i * frame_secs
        d.on_frame("system", _frame(frame_secs, sr), t, t)
    # Mic triggers a segment at start_ts=0.5 (mono time 100.5) — earlier
    # than system's pre-buffer can reach.
    d.on_segment(SpeechSegment("mic", 0.5, 3.0), now=103.0)
    buffers = d._buffers
    assert buffers is not None
    # session_t0_mono = 100.5. Both channels must span [100.5, ~103.0].
    assert abs(buffers.session_t0_mono - 100.5) < 1e-3
    # mic_pcm: 2.5 seconds (from pre-buffer, samples 0.5s→3.0s).
    assert abs(len(buffers.mic_pcm) / 2 / sr - 2.5) < 0.05
    # sys_pcm: 2.5 seconds total, where the first 0.5s is zero-padded (sys
    # started at 101.0, pad covers [100.5, 101.0]), plus 2.0s of real.
    assert abs(len(buffers.sys_pcm) / 2 / sr - 2.5) < 0.05
    # Verify the pad is actually zeros at the front.
    pad_samples = int(0.5 * sr)
    pad_bytes = bytes(buffers.sys_pcm[: pad_samples * 2])
    arr = np.frombuffer(pad_bytes, dtype=np.int16)
    assert np.all(arr == 0), "front pad on sys_pcm should be zeros"


def test_on_frame_zero_fills_dropped_frame_gap():
    """When a frame arrives late (gap > tolerance), the detector zero-fills
    the gap so the sample-to-mono-time invariant holds."""
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    # Push 1 second of frames normally (in lockstep with a nominal 20ms
    # cadence).
    for i in range(int(1.0 / frame_secs)):
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    pre_len_before = len(d._pre_buffers["mic"])
    # Simulate a 200ms gap: next frame's capture_mono_ts jumps ahead by
    # 0.2s + nominal 20ms instead of just 20ms.
    t_gap = 100.0 + 1.0 + 0.2  # 200ms gap
    d.on_frame("mic", _frame(frame_secs, sr), t_gap, t_gap)
    pre_len_after = len(d._pre_buffers["mic"])
    # Expected: buffer grew by (gap + frame) * sr samples = 220ms * sr samples.
    expected_growth_samples = int(0.22 * sr)
    actual_growth_samples = (pre_len_after - pre_len_before) // 2
    # Allow ±1 frame of rounding.
    assert abs(actual_growth_samples - expected_growth_samples) <= int(frame_secs * sr) + 1, (
        f"expected ~{expected_growth_samples} samples of growth from gap-fill + frame, "
        f"got {actual_growth_samples}"
    )


def test_close_pads_shorter_buffer_to_match():
    """On session close, mic_pcm and sys_pcm must be equal length. If one
    source kept capturing longer than the other (e.g. mic died mid-session),
    the shorter buffer is zero-padded at the tail."""
    sr = 16000
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0), sample_rate=sr)
    frame_secs = 0.02
    # Feed mic and sys for 2 seconds each, in lockstep (epochs anchored).
    for i in range(int(2.0 / frame_secs)):
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
        d.on_frame("system", _frame(frame_secs, sr), t, t)
    # Session opens on mic segment.
    d.on_segment(SpeechSegment("mic", 0.0, 2.0), now=102.0)
    # Continue feeding mic past the point where sys goes silent.
    for i in range(int(1.0 / frame_secs)):
        t = 102.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    # sys gets no more frames. At close, sys_pcm should be padded to match.
    d.on_segment(SpeechSegment("mic", 2.5, 3.0), now=103.0)  # keep session alive
    d.tick(110.0)  # past joint silence
    closed = d.take_closed_session()
    assert closed is not None
    assert len(closed.mic_pcm) == len(closed.sys_pcm), (
        f"mic_pcm ({len(closed.mic_pcm)}) and sys_pcm ({len(closed.sys_pcm)}) must match"
    )
    # session_end_mono must reflect the actual PCM duration.
    audio_dur = len(closed.mic_pcm) / 2 / sr
    assert abs((closed.session_end_mono - closed.session_t0_mono) - audio_dur) < 0.01
