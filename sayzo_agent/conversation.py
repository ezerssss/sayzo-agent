"""Silence-bounded session detector and substantive-user-turn gate.

This module is intentionally pure (no audio I/O, no model loading) so it can
be unit-tested with synthetic VAD events.

Session lifecycle
-----------------
Sessions open the moment the agent arms — ``open_session_on_arm(now)`` is
called by ``ArmController._arm_internal`` right after ``reset_source_epochs``
and before either capture stream actually starts. Frames that arrive while
the detector is IDLE are dropped on the floor; in armed-only mode, the only
producer of frames (mic / sys captures) is gated behind ``armed_event``, so
IDLE means "nothing should be coming through" and any frame that does is
either a stale leftover from a previous arm cycle or a logic bug worth
ignoring.

Mono-clock invariant
--------------------
Every frame passed to ``on_frame`` carries a ``capture_mono_ts`` — the
``time.monotonic()`` value of the first sample in the frame. While a session
is OPEN, sample ``N`` of ``mic_pcm`` / ``sys_pcm`` corresponds to monotonic
time ``session_t0_mono + N/sample_rate``. Modest dropped / late frames
(scheduler hiccups, USB jitter) are zero-filled to preserve that invariant.
Implausibly large gaps (capped by ``ConversationConfig.max_gap_fill_secs``)
re-anchor instead of filling — a 200 s "gap" is never a real audio dropout,
it's stale state, and zero-filling it would inject 200 s of silence into the
session.

Test-path compatibility: if ``on_segment`` is called without any preceding
``on_frame`` calls on a source (i.e., ``_source_epoch_mono[src]`` is None),
the detector falls back to the old behavior where VAD-sample-second
timestamps are treated as if already on a shared timeline. The synthetic
tests in ``tests/test_conversation.py`` rely on this.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

import numpy as np

from .config import ConversationConfig
from .models import SessionBuffers, SessionCloseReason, SpeechSegment, Source

log = logging.getLogger(__name__)


class SessionState(str, Enum):
    IDLE = "idle"
    OPEN = "open"
    # Joint silence has been observed; the session's buffers are held and
    # an end-confirmation prompt is being shown. The session may either
    # commit (become closed) or revert (go back to OPEN with a reset silence
    # timer) based on the user's answer.
    PENDING_CLOSE = "pending_close"


@dataclass
class GateResult:
    passed: bool
    reason: str
    mic_total: float
    mic_max_turn: float
    mic_turn_count: int
    sys_total: float


class ConversationDetector:
    """State machine that opens/closes sessions based on joint silence.

    Inputs (push API):
        - on_frame(source, frame, capture_mono_ts, now): raw PCM written to
          the active session buffer if a session is open, or to the rolling
          pre-buffer if idle. ``capture_mono_ts`` is the monotonic time of
          the frame's first sample; used for gap-fill and cross-source
          alignment.
        - on_segment(seg, now): a SpeechSegment closed by the VAD on
          ``seg.source``. Opens a session if idle, refreshes the joint-
          silence timer, and appends to the session's segment list.
        - tick(now_monotonic): called periodically to check for session
          close via sustained joint silence or safety cap.

    Outputs (pull API):
        - take_closed_session() -> SessionBuffers | None
    """

    def __init__(self, config: ConversationConfig, sample_rate: int = 16000) -> None:
        self.cfg = config
        self.sample_rate = sample_rate
        self.state = SessionState.IDLE
        self._buffers: Optional[SessionBuffers] = None
        self._closed_queue: list[SessionBuffers] = []
        # Called when the detector transitions OPEN → PENDING_CLOSE on joint
        # silence. The ArmController subscribes to this and shows the end-
        # confirmation toast; on the user's answer it calls `commit_close()`
        # or `revert_close()`. If no callback is registered (unit tests), the
        # detector commits immediately — preserving the legacy behavior where
        # joint silence directly closed the session.
        self.on_pending_close: Optional[Callable[[], None]] = None
        # Track the last voiced wall time on each source (monotonic seconds).
        self._last_voiced_mono: dict[Source, float] = {"mic": 0.0, "system": 0.0}
        self._session_start_mono: float = 0.0
        # Monotonic-clock anchoring for each source. Set on the SECOND frame
        # received (the first frame often carries device-priming stall); never
        # modified afterward within a session. Used to convert VAD sample
        # indices (which are in "real samples since this source started"
        # units) into absolute monotonic seconds:
        # ``mono(vad_sample) = _source_epoch_mono[src] + vad_sample / sr``.
        self._source_epoch_mono: dict[Source, Optional[float]] = {"mic": None, "system": None}
        self._source_frames_seen: dict[Source, int] = {"mic": 0, "system": 0}
        # Mono time of the tail of each source's session buffer. Advances by
        # frame_duration on each append and by gap on each zero-fill.
        self._stream_end_mono: dict[Source, Optional[float]] = {"mic": None, "system": None}
        # Monotonic time of sample 0 of ``session_pcm[src]`` once a session
        # opens. Equal to ``now`` at ``open_session_on_arm`` time.
        self._session_t0_mono: float = 0.0

    # ---- helpers -----------------------------------------------------------

    def _gap_tolerance(self, source: Source) -> float:
        if source == "mic":
            return self.cfg.gap_tolerance_secs_mic
        return self.cfg.gap_tolerance_secs_system

    def _seg_mono(self, source: Source, vad_ts: float) -> float:
        """Convert a VAD-derived sample-second timestamp to monotonic time.

        VAD's internal sample counter is never reset, so ``vad_ts`` is in
        "seconds since that source's VAD started". We add the source's
        epoch to get absolute monotonic seconds. When no frames have been
        seen (test path), treat ``vad_ts`` as already on a shared timeline
        (equivalent to epoch=0).
        """
        epoch = self._source_epoch_mono[source]
        if epoch is None:
            return vad_ts
        return epoch + vad_ts

    # ---- session lifecycle -------------------------------------------------

    def open_session_on_arm(self, now: float) -> None:
        """Open a session immediately at arm time, with empty buffers.

        Production entrypoint: ``ArmController._arm_internal`` calls this
        right after ``reset_source_epochs`` and before opening capture
        streams. ``session_t0_mono`` is anchored to ``now`` (the arm
        timestamp); subsequent frames flow directly into ``mic_pcm`` /
        ``sys_pcm`` via ``on_frame`` with gap-fill bridging the small
        delay between arm and the first real frame.

        Idempotent for repeated calls while OPEN — does nothing.
        """
        if self.state != SessionState.IDLE:
            return
        self._open_session(now, trigger=None, trigger_start_ts=0.0, t0_mono=now)

    def _open_session(
        self,
        now: float,
        trigger: Optional[Source],
        trigger_start_ts: float,
        t0_mono: Optional[float] = None,
    ) -> None:
        self.state = SessionState.OPEN
        self._buffers = SessionBuffers(
            started_at=datetime.now(timezone.utc),
            started_monotonic=now,
        )
        self._session_start_mono = now
        self._last_voiced_mono["mic"] = now if trigger == "mic" else 0.0
        self._last_voiced_mono["system"] = now if trigger == "system" else 0.0
        # Session origin in monotonic time. Arm-triggered: anchored to ``now``
        # (the arm timestamp). VAD-triggered (legacy / test path): anchored
        # to the segment's mono time.
        if t0_mono is not None:
            session_t0 = t0_mono
        else:
            assert trigger is not None
            session_t0 = self._seg_mono(trigger, trigger_start_ts)
        self._session_t0_mono = session_t0
        self._buffers.session_t0_mono = session_t0

        # Both source streams start empty at session_t0; the first real frame
        # on each source will gap-fill from session_t0 to its capture_mono_ts
        # via the on_frame path.
        self._stream_end_mono = {"mic": session_t0, "system": session_t0}

        log.info(
            "[session] OPENED at +0.00s (trigger=%s)",
            trigger or "arm",
        )

    def _close_session(self, now: float, reason: SessionCloseReason) -> None:
        if self._buffers is None:
            return
        self._buffers.ended_monotonic = now
        self._buffers.close_reason = reason

        # Equalize mic_pcm and sys_pcm lengths: zero-pad the shorter at the
        # tail so both channels share the same session_end_mono. The
        # difference is bounded by joint_silence_close_secs (tail silence the
        # VAD already filtered out), so this injects no real segment content.
        mic_len = len(self._buffers.mic_pcm)
        sys_len = len(self._buffers.sys_pcm)
        if mic_len < sys_len:
            self._buffers.mic_pcm.extend(b"\x00" * (sys_len - mic_len))
        elif sys_len < mic_len:
            self._buffers.sys_pcm.extend(b"\x00" * (mic_len - sys_len))
        # After equalization: both buffers cover [session_t0_mono,
        # session_t0_mono + N/sr] where N = len(mic_pcm)/2.
        total_samples = len(self._buffers.mic_pcm) // 2
        self._buffers.session_end_mono = (
            self._buffers.session_t0_mono + total_samples / self.sample_rate
        )

        elapsed = self._buffers.elapsed()
        log.info(
            "[session] CLOSED after %.1fs reason=%s mic_total=%.1fs (max_turn=%.1fs over %d turns) sys_total=%.1fs audio_dur=%.1fs",
            elapsed,
            reason.value,
            self._buffers.mic_total_voiced(),
            self._buffers.mic_max_turn(),
            self._buffers.mic_turn_count(),
            self._buffers.sys_total_voiced(),
            self._buffers.pcm_duration(self.sample_rate),
        )
        self._closed_queue.append(self._buffers)
        self._buffers = None
        self.state = SessionState.IDLE
        self._last_voiced_mono = {"mic": 0.0, "system": 0.0}
        self._session_t0_mono = 0.0
        # Source-level stream_end_mono is no longer meaningful post-close;
        # the next ``open_session_on_arm`` re-anchors it to that arm time.
        self._stream_end_mono = {"mic": None, "system": None}

    # ---- input API ---------------------------------------------------------

    def on_frame(
        self,
        source: Source,
        frame: np.ndarray,
        capture_mono_ts: float,
        now: float,
    ) -> None:
        """Append raw PCM to the active session buffer.

        Frames received while the detector is IDLE are dropped on the
        floor — in armed-only mode the capture streams are gated behind
        ``armed_event`` and ``open_session_on_arm`` is called before they
        start, so any IDLE frame is either a stale leftover from a
        previous arm cycle (mic.queue not fully drained) or post-close
        bleed-through. Either way it must not pollute the next session.

        ``capture_mono_ts`` is the monotonic time of the frame's first
        sample. Modest gaps between successive frames are zero-filled to
        preserve the sample-to-mono-time invariant. Implausibly large
        gaps (capped by ``ConversationConfig.max_gap_fill_secs``) re-
        anchor the stream rather than filling — a multi-second "gap" is
        never a real audio dropout, it's stale state, and zero-filling
        it would corrupt the session timeline.
        """
        if (
            self.state not in (SessionState.OPEN, SessionState.PENDING_CLOSE)
            or self._buffers is None
        ):
            return

        # int16 little-endian for compact buffering
        pcm16 = np.clip(frame * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        frame_samples = len(frame)
        frame_duration = frame_samples / self.sample_rate

        # Anchor epoch on the SECOND frame — the first frame after stream
        # .start() often carries device-priming jitter (PortAudio startup
        # buffer, pipe-read warmup). Skipping it costs ~one frame of
        # fidelity and buys a stable anchor for VAD-segment timestamping.
        self._source_frames_seen[source] += 1
        if self._source_epoch_mono[source] is None and self._source_frames_seen[source] >= 2:
            first_frame_duration = frame_duration  # assume uniform
            self._source_epoch_mono[source] = capture_mono_ts - first_frame_duration

        dst = self._buffers.mic_pcm if source == "mic" else self._buffers.sys_pcm
        stream_end = self._stream_end_mono[source]
        if stream_end is None:
            stream_end = capture_mono_ts

        gap_secs = capture_mono_ts - stream_end
        gap_tolerance = self._gap_tolerance(source)
        max_fill = self.cfg.max_gap_fill_secs
        if gap_secs > max_fill:
            # Implausibly large gap — likely a stale frame from before this
            # arm cycle, or the wall clock skipped (system suspend, USB
            # reconnect). Re-anchor instead of filling: the new sample 0 of
            # what's appended below corresponds to capture_mono_ts, with no
            # zero-fill bridge. Leaves a small audible discontinuity, which
            # is far better than injecting minutes of silence.
            log.warning(
                "[session] %s: dropping %.1fs gap-fill (cap=%.1fs) — re-anchoring",
                source, gap_secs, max_fill,
            )
            stream_end = capture_mono_ts
        elif gap_secs > gap_tolerance:
            gap_samples = int(round(gap_secs * self.sample_rate))
            if gap_samples > 0:
                dst.extend(b"\x00\x00" * gap_samples)
                stream_end = stream_end + gap_samples / self.sample_rate

        dst.extend(pcm16)
        stream_end = stream_end + frame_duration
        self._stream_end_mono[source] = stream_end

    def on_segment(self, seg: SpeechSegment, now: float) -> None:
        """Register a closed VAD segment on `seg.source`.

        If the detector is in PENDING_CLOSE when a segment arrives, the user
        has resumed speaking during the end-confirmation toast window — the
        pending close auto-reverts so the meeting continues as one capture.
        """
        if self.state == SessionState.PENDING_CLOSE:
            log.info(
                "[session] PENDING_CLOSE auto-reverted (VAD speech on %s)",
                seg.source,
            )
            self.revert_close(now)
        if self.state == SessionState.IDLE:
            self._open_session(now, seg.source, seg.start_ts)
            assert self._buffers is not None
        assert self._buffers is not None
        # Convert VAD sample-seconds → monotonic time → session-relative
        # seconds. This correctly handles the case where mic and system VAD
        # sample counters have drifted relative to each other (since both
        # are anchored by their own `_source_epoch_mono`).
        seg_start_mono = self._seg_mono(seg.source, seg.start_ts)
        seg_end_mono = self._seg_mono(seg.source, seg.end_ts)
        rebased = SpeechSegment(
            source=seg.source,
            start_ts=max(0.0, seg_start_mono - self._session_t0_mono),
            end_ts=max(0.0, seg_end_mono - self._session_t0_mono),
        )
        if seg.source == "mic":
            self._buffers.mic_segments.append(rebased)
        else:
            self._buffers.sys_segments.append(rebased)
        self._last_voiced_mono[seg.source] = now
        log.debug(
            "[vad] %s speech %.2f→%.2f (%.1fs)",
            seg.source,
            seg.start_ts,
            seg.end_ts,
            seg.duration,
        )

    def tick(self, now: float) -> None:
        """Periodic check for session close via joint silence.

        On joint silence, the detector transitions OPEN → PENDING_CLOSE and
        calls `on_pending_close` so the ArmController can show the end-
        confirmation toast. The session's PCM buffers are held in memory;
        nothing is written to disk yet. The caller resolves via
        `commit_close()` ("Yes, done"), `revert_close()` ("Not yet"), or
        lets a VAD segment auto-revert (user resumed speaking).

        Unit-test compatibility: when no `on_pending_close` callback is
        registered, the detector commits immediately, preserving the legacy
        behavior where joint silence directly closed the session.
        """
        if self.state != SessionState.OPEN or self._buffers is None:
            return

        # Joint silence: both sources have been quiet for >= threshold
        last_any = max(self._last_voiced_mono["mic"], self._last_voiced_mono["system"])
        if last_any > 0 and (now - last_any) >= self.cfg.joint_silence_close_secs:
            self.state = SessionState.PENDING_CLOSE
            log.info(
                "[session] PENDING_CLOSE (joint silence %.1fs) — awaiting user confirmation",
                now - last_any,
            )
            if self.on_pending_close is not None:
                try:
                    self.on_pending_close()
                except Exception:
                    log.exception("[session] on_pending_close callback raised")
            else:
                # Legacy test path: no arm controller attached, commit immediately.
                self.commit_close(now, SessionCloseReason.JOINT_SILENCE)

    def commit_close(
        self, now: float, reason: SessionCloseReason = SessionCloseReason.JOINT_SILENCE
    ) -> None:
        """Finalize a PENDING_CLOSE or OPEN session and hand buffers to the
        closed queue. Called by the ArmController after the end-confirmation
        toast resolves with "Yes, done", or from `force_close()`.

        Safe to call from either OPEN (test-path / shutdown) or PENDING_CLOSE.
        """
        if self.state == SessionState.IDLE or self._buffers is None:
            return
        self._close_session(now, reason)

    def revert_close(self, now: float) -> None:
        """Cancel a pending close — the user said "Not yet", or a VAD segment
        arrived during the confirmation window. Session goes back to OPEN
        with the silence timer reset so the next 45 s silence would re-trigger
        `PENDING_CLOSE` afresh."""
        if self.state != SessionState.PENDING_CLOSE or self._buffers is None:
            return
        self.state = SessionState.OPEN
        # Reset silence clocks so `tick` doesn't immediately re-enter
        # PENDING_CLOSE on the very next call. `now` is treated as
        # "voice-equivalent" on both sources — the next real VAD segment
        # will overwrite these as usual.
        self._last_voiced_mono["mic"] = now
        self._last_voiced_mono["system"] = now
        log.info("[session] PENDING_CLOSE reverted — continuing session")

    def force_close(self, now: float) -> None:
        """Force-close regardless of current state. Used on agent shutdown;
        also commits a PENDING_CLOSE session so in-flight audio is preserved
        on exit rather than discarded.
        """
        if self.state in (SessionState.OPEN, SessionState.PENDING_CLOSE):
            self._close_session(now, SessionCloseReason.SHUTDOWN)

    def reset_source_epochs(self) -> None:
        """Reset the per-source clock anchors so the next frame behaves as if
        the source started fresh. Called by the ArmController on every
        disarm → arm transition, alongside SileroVAD.reset(). Together they
        ensure re-armed sessions don't carry stale epoch anchors from a
        previously-armed cold-session that has long since ended.

        Must only be called while IDLE.
        """
        if self.state != SessionState.IDLE:
            raise RuntimeError(
                f"reset_source_epochs requires state=IDLE, got {self.state}"
            )
        self._source_epoch_mono = {"mic": None, "system": None}
        self._source_frames_seen = {"mic": 0, "system": 0}
        self._stream_end_mono = {"mic": None, "system": None}

    # ---- output API --------------------------------------------------------

    def take_closed_session(self) -> Optional[SessionBuffers]:
        if not self._closed_queue:
            return None
        return self._closed_queue.pop(0)


def evaluate_user_turn_gate(buffers: SessionBuffers, cfg: ConversationConfig) -> GateResult:
    """Cheap pre-STT gate: substantive user turn AND counterparty present."""
    mic_total = buffers.mic_total_voiced()
    mic_max = buffers.mic_max_turn()
    mic_turns = buffers.mic_turn_count()
    sys_total = buffers.sys_total_voiced()

    has_long_turn = mic_max >= cfg.min_user_turn_secs
    has_cumulative = (
        mic_total >= cfg.min_user_total_secs
        and mic_turns >= cfg.min_user_turns_for_total
    )
    user_ok = has_long_turn or has_cumulative
    sys_ok = sys_total >= cfg.min_sys_voiced_secs

    if not user_ok:
        reason = (
            f"FAIL substantive-user-turn (max_turn={mic_max:.1f}s < {cfg.min_user_turn_secs:.0f}s "
            f"AND cumulative={mic_total:.1f}s/{mic_turns} turns < "
            f"{cfg.min_user_total_secs:.0f}s/{cfg.min_user_turns_for_total})"
        )
    elif not sys_ok:
        reason = (
            f"FAIL counterparty (sys_total={sys_total:.1f}s < {cfg.min_sys_voiced_secs:.1f}s — "
            f"no other side)"
        )
    else:
        if has_long_turn:
            reason = f"PASS substantive-user-turn (max_turn={mic_max:.1f}s ≥ {cfg.min_user_turn_secs:.0f}s)"
        else:
            reason = (
                f"PASS substantive-user-turn (cumulative={mic_total:.1f}s "
                f"over {mic_turns} turns)"
            )

    return GateResult(
        passed=user_ok and sys_ok,
        reason=reason,
        mic_total=mic_total,
        mic_max_turn=mic_max,
        mic_turn_count=mic_turns,
        sys_total=sys_total,
    )


def merge_close_segments(
    segments: list[SpeechSegment],
    gap_secs: float,
) -> list[SpeechSegment]:
    """Merge time ranges whose inter-segment gap is shorter than `gap_secs`.

    Used by the final-audio trimmer to preserve conversational pauses
    (thinking beats, response latency) as real audio while still removing
    true dead air. Ignores `source` — two segments from different sources
    are merged if their timestamps are close enough, and the resulting
    merged segment is tagged as "mic" purely so the SpeechSegment dataclass
    is satisfied (build_windowed_pcm doesn't look at the source field).

    Pure / unit-testable. Returns a new sorted list; input is not mutated.
    `gap_secs <= 0` disables merging and just sorts the input.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s.start_ts)
    if gap_secs <= 0:
        return ordered
    merged: list[SpeechSegment] = [
        SpeechSegment(source=ordered[0].source, start_ts=ordered[0].start_ts, end_ts=ordered[0].end_ts)
    ]
    for s in ordered[1:]:
        last = merged[-1]
        if s.start_ts - last.end_ts <= gap_secs:
            if s.end_ts > last.end_ts:
                merged[-1] = SpeechSegment(
                    source=last.source,
                    start_ts=last.start_ts,
                    end_ts=s.end_ts,
                )
        else:
            merged.append(
                SpeechSegment(source=s.source, start_ts=s.start_ts, end_ts=s.end_ts)
            )
    return merged


def build_windowed_pcm(
    pcm: bytes,
    keep_segments: list[SpeechSegment],
    pad_secs: float,
    sample_rate: int,
) -> bytes:
    """Return a copy of `pcm` with everything OUTSIDE the merged
    [seg.start_ts - pad, seg.end_ts + pad] windows zero-filled.

    `pcm` is little-endian int16 mono at `sample_rate`. Timestamps are
    preserved 1:1 so the windowed buffer can be passed to STT and the
    resulting word timestamps will line up with the original session
    timeline. Pure / unit-testable.
    """
    if not pcm:
        return b""
    if not keep_segments:
        return bytes(len(pcm))  # all zeros, same length

    bytes_per_sample = 2
    total_samples = len(pcm) // bytes_per_sample

    # Build merged windows in sample units
    pad_samples = int(pad_secs * sample_rate)
    raw: list[tuple[int, int]] = []
    for seg in keep_segments:
        a = max(0, int(seg.start_ts * sample_rate) - pad_samples)
        b = min(total_samples, int(seg.end_ts * sample_rate) + pad_samples)
        if b > a:
            raw.append((a, b))
    if not raw:
        return bytes(len(pcm))

    raw.sort()
    merged: list[list[int]] = [list(raw[0])]
    for a, b in raw[1:]:
        if a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])

    out = bytearray(len(pcm))
    for a, b in merged:
        byte_a = a * bytes_per_sample
        byte_b = b * bytes_per_sample
        out[byte_a:byte_b] = pcm[byte_a:byte_b]
    return bytes(out)
