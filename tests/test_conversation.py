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


def test_on_frame_drops_when_idle():
    """Regression: in armed-only mode, frames received while IDLE must be
    dropped on the floor — there is no pre-buffer, and any frame seen
    while IDLE is either a stale leftover from a previous arm cycle or
    post-close bleed-through. Either way it must not pollute the next
    session.
    """
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    for i in range(int(5.0 / frame_secs)):
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
        d.on_frame("system", _frame(frame_secs, sr), t, t)
    # State stays IDLE; nothing is buffered (no pre-buffer in the new model).
    assert d.state == SessionState.IDLE
    assert d._buffers is None
    # Open session at arm time; buffers start empty.
    d.open_session_on_arm(now=200.0)
    assert d.state == SessionState.OPEN
    assert d._buffers is not None
    assert len(d._buffers.mic_pcm) == 0
    assert len(d._buffers.sys_pcm) == 0


def test_open_session_on_arm_idempotent_when_open():
    """Calling open_session_on_arm a second time while already OPEN must
    not reset the in-flight session."""
    d = ConversationDetector(_cfg())
    d.open_session_on_arm(now=100.0)
    first_buffers = d._buffers
    assert first_buffers is not None
    d.open_session_on_arm(now=110.0)
    # Same buffers object — second call was a no-op.
    assert d._buffers is first_buffers


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
    d._stream_end_mono["mic"] = 105.0
    d.reset_source_epochs()
    assert d._source_epoch_mono == {"mic": None, "system": None}
    assert d._source_frames_seen == {"mic": 0, "system": 0}
    assert d._stream_end_mono == {"mic": None, "system": None}


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


def test_session_buffers_aligned_when_sources_start_at_different_times():
    """Mic and sys streams come up at slightly different mono times after
    arm. The session opens at arm_now, and each source's first real frame
    gap-fills from arm_now to its own capture_mono_ts. Both buffers must
    end up the same length once they've each received the same number of
    real seconds of frames.
    """
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    arm_now = 100.0
    d.open_session_on_arm(now=arm_now)
    # Mic's first real frame arrives 100 ms after arm; sys's 600 ms after.
    # Both then stream 2 seconds of frames in lockstep.
    n_frames = int(2.0 / frame_secs)
    for i in range(n_frames):
        t_mic = arm_now + 0.10 + i * frame_secs
        t_sys = arm_now + 0.60 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t_mic, t_mic)
        d.on_frame("system", _frame(frame_secs, sr), t_sys, t_sys)
    buffers = d._buffers
    assert buffers is not None
    assert buffers.session_t0_mono == arm_now
    # Mic: 100 ms of zero-fill + 2 s real = ~2.10 s.
    mic_dur = len(buffers.mic_pcm) / 2 / sr
    assert abs(mic_dur - 2.10) < 0.05, f"mic_pcm = {mic_dur:.3f}s (expected ~2.10s)"
    # Sys: 600 ms of zero-fill + 2 s real = ~2.60 s.
    sys_dur = len(buffers.sys_pcm) / 2 / sr
    assert abs(sys_dur - 2.60) < 0.05, f"sys_pcm = {sys_dur:.3f}s (expected ~2.60s)"
    # Both buffers anchored to the same session_t0_mono: equal-index
    # samples correspond to the same wall-clock moment, even though the
    # buffers have different lengths (sys is 500 ms behind).


def test_in_session_gap_fill_zero_fills_dropped_frame():
    """Modest scheduler-jitter gaps within a session are zero-filled so
    the sample-to-mono-time invariant holds."""
    sr = 16000
    d = ConversationDetector(_cfg(), sample_rate=sr)
    frame_secs = 0.02
    arm_now = 100.0
    d.open_session_on_arm(now=arm_now)
    # 1 s of frames in lockstep starting right at arm.
    for i in range(int(1.0 / frame_secs)):
        t = arm_now + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    len_before = len(d._buffers.mic_pcm)
    # 200 ms gap, then a frame.
    t_gap = arm_now + 1.0 + 0.2
    d.on_frame("mic", _frame(frame_secs, sr), t_gap, t_gap)
    len_after = len(d._buffers.mic_pcm)
    expected_growth_samples = int(0.22 * sr)  # 200 ms zero-fill + 20 ms frame
    actual_growth_samples = (len_after - len_before) // 2
    assert abs(actual_growth_samples - expected_growth_samples) <= int(frame_secs * sr) + 1


def test_in_session_gap_fill_caps_at_max_and_reanchors():
    """An implausibly large gap (e.g. stale frame from before this arm
    cycle, or system suspend) must NOT be zero-filled into oblivion. The
    detector re-anchors instead, so the session timeline doesn't end up
    minutes longer than the actual wall-clock event.

    Direct regression guard for the bug where stale frames in mic.queue
    crossing a 200-second disarm gap caused the detector to inject 200 s
    of zeros into mic_pcm.
    """
    sr = 16000
    cfg = _cfg(max_gap_fill_secs=2.0)
    d = ConversationDetector(cfg, sample_rate=sr)
    frame_secs = 0.02
    arm_now = 100.0
    d.open_session_on_arm(now=arm_now)
    # 1 s of normal frames.
    for i in range(int(1.0 / frame_secs)):
        t = arm_now + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    len_before = len(d._buffers.mic_pcm)
    # Now a frame arrives 200 s in the future (the bug scenario, just
    # mirrored: same effect — gap exceeds the cap).
    t_runaway = arm_now + 1.0 + 200.0
    d.on_frame("mic", _frame(frame_secs, sr), t_runaway, t_runaway)
    len_after = len(d._buffers.mic_pcm)
    # No 200 s of zero-fill — only the 20 ms of the frame itself was added.
    growth_samples = (len_after - len_before) // 2
    assert growth_samples <= int(frame_secs * sr) + 1, (
        f"runaway gap-fill: buffer grew by {growth_samples} samples; "
        f"expected ~{int(frame_secs * sr)} (one frame, no fill)"
    )


def test_stale_frames_across_arm_boundary_do_not_inject_zeros():
    """End-to-end regression for the v2.1.5 bug: a stale frame whose
    capture_mono_ts is from a previous arm cycle (3 minutes ago), followed
    by a normal frame from the new arm, must not result in 200 seconds of
    silence at the start of the session.

    Models the real failure mode: ``mic.queue`` retained frames from
    before disarm, ``armed_event`` blocked the consumer for 200 s, and on
    re-arm the consumer pulled the stale frames first, then the new
    frames — producing a ~200 s gap-fill in the previous code path.
    """
    sr = 16000
    cfg = _cfg(max_gap_fill_secs=2.0)
    d = ConversationDetector(cfg, sample_rate=sr)
    frame_secs = 0.02

    # ---- prior arm cycle: open + close a session at t≈100. ----
    d.open_session_on_arm(now=100.0)
    for i in range(int(0.5 / frame_secs)):
        t = 100.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    d.commit_close(101.0, SessionCloseReason.WHITELIST_ENDED)
    assert d.state == SessionState.IDLE

    # ---- 200 s of disarm. The detector resets epoch state on re-arm. ----
    d.reset_source_epochs()
    arm_now = 300.0
    d.open_session_on_arm(now=arm_now)

    # ---- a stale frame slips through (timestamped from 200 s ago). ----
    stale_ts = 100.5
    d.on_frame("mic", _frame(frame_secs, sr), stale_ts, arm_now)
    # Then real frames at the NEW arm time.
    for i in range(int(0.5 / frame_secs)):
        t = arm_now + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)

    # mic_pcm must NOT contain 200 seconds of zeros — at most a fraction
    # of a second of either fill or junk from the stale frame.
    mic_dur = len(d._buffers.mic_pcm) / 2 / sr
    assert mic_dur < 1.0, (
        f"stale-frame regression: mic_pcm grew to {mic_dur:.1f}s, expected < 1s. "
        "200-second zero-fill is back."
    )


def test_close_pads_shorter_buffer_to_match():
    """On session close, mic_pcm and sys_pcm must be equal length. If one
    source kept capturing longer than the other (e.g. mic died mid-session),
    the shorter buffer is zero-padded at the tail."""
    sr = 16000
    d = ConversationDetector(_cfg(joint_silence_close_secs=5.0), sample_rate=sr)
    frame_secs = 0.02
    arm_now = 100.0
    d.open_session_on_arm(now=arm_now)
    # Feed mic and sys for 2 seconds each, in lockstep.
    for i in range(int(2.0 / frame_secs)):
        t = arm_now + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
        d.on_frame("system", _frame(frame_secs, sr), t, t)
    # Continue feeding mic past the point where sys goes silent.
    for i in range(int(1.0 / frame_secs)):
        t = arm_now + 2.0 + i * frame_secs
        d.on_frame("mic", _frame(frame_secs, sr), t, t)
    # Add a couple of segments so the session has voice activity.
    d.on_segment(SpeechSegment("mic", 0.0, 2.0), now=arm_now + 2.0)
    d.on_segment(SpeechSegment("mic", 2.5, 3.0), now=arm_now + 3.0)
    d.tick(arm_now + 10.0)  # past joint silence
    closed = d.take_closed_session()
    assert closed is not None
    assert len(closed.mic_pcm) == len(closed.sys_pcm), (
        f"mic_pcm ({len(closed.mic_pcm)}) and sys_pcm ({len(closed.sys_pcm)}) must match"
    )
    # session_end_mono must reflect the actual PCM duration.
    audio_dur = len(closed.mic_pcm) / 2 / sr
    assert abs((closed.session_end_mono - closed.session_t0_mono) - audio_dur) < 0.01
