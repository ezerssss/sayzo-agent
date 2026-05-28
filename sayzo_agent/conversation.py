"""Silence-bounded session detector and substantive-user-turn gate.

This module is intentionally pure (no audio I/O, no model loading) so it can
be unit-tested with synthetic VAD events.

Session lifecycle
-----------------
Sessions open the moment the agent arms — ``open_session_on_arm(now)`` is
called by ``ArmController._arm_internal`` right after ``reset_per_source_streams``
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
time ``session_t0_mono + N/sample_rate``. The invariant is load-bearing for
the AEC pre-pass: mic[k] and sys[k] must represent the same wall-clock
moment, otherwise AEC3's ±500 ms search window can't find the alignment.

To preserve the invariant:
  * Modest dropped / late frames (scheduler jitter) are zero-filled.
  * **Cold-start gaps** between arm time and the first frame on a source
    are also zero-filled, even if they're several seconds long. The
    system-capture thread typically takes 1–5 s to open WASAPI, prime the
    silence pump, and deliver its first 500 ms batch; mic usually arrives
    in well under a second. Without zero-fill bridging the asymmetry, the
    two sources end up offset by exactly the difference in their startup
    delays. ``ConversationConfig.max_gap_fill_secs`` (default 30 s in
    v3.6+) is the upper bound; above it the detector re-anchors so a
    literal system-suspend can't inject minutes of silence.
  * **Stale frames** (``capture_mono_ts < session_t0_mono``) — frames left
    in the producer queue from a previous arm cycle that leak through
    after re-arm — are detected explicitly and dropped. Before v3.6 the
    re-anchor branch did double duty as the stale-frame guard, and the
    2 s cap was tuned for that role; the result was that legitimate
    cold-start gaps above 2 s also hit the re-anchor path without
    zero-fill, silently breaking AEC.

VAD segment contract: ``on_segment(seg, now)`` expects ``seg.start_ts``
and ``seg.end_ts`` in **monotonic seconds** (the values
``SileroVAD.feed`` produces by anchoring chunks to the ``frame_mono_ts``
passed in). The detector rebases by subtracting ``_session_t0_mono`` so
buffered segments come out session-relative. Synthetic tests that pass
``SpeechSegment("mic", 0.0, 1.0)`` directly to ``on_segment`` without
preceding ``on_frame`` calls still work: when state is IDLE,
``_open_session`` anchors ``session_t0`` to ``seg.start_ts`` so the
rebase ends up at 0.
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


# Tolerance for the stale-frame check in ``on_frame``: a frame whose
# ``capture_mono_ts`` falls within this many seconds before ``session_t0_mono``
# is treated as a normal first-frame arrival (the arm boundary is fuzzy at
# the millisecond level — capture threads stamp the first sample slightly
# before ArmController records ``now`` for the open call). Anything earlier
# than that is a genuine leak from a previous arm cycle and dropped.
_STALE_FRAME_JITTER_SECS = 0.5


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
        - on_frame(source, frame, capture_mono_ts, now): raw PCM written
          to the active session buffer if a session is OPEN or
          PENDING_CLOSE; dropped on the floor when IDLE. (Pre-v2.1.7
          there was a rolling pre-buffer for the always-on model — removed
          when sessions opened on arm rather than on first VAD segment;
          see [[project_no_pre_buffer]].) ``capture_mono_ts`` is the
          monotonic time of the frame's first sample; used for gap-fill
          and cross-source alignment.
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
        # Mono time of the tail of each source's session buffer. Advances by
        # frame_duration on each append and by gap on each zero-fill. Used
        # only by ``on_frame`` for gap-fill / re-anchor decisions; VAD-side
        # timestamping is independent (segments carry their own mono times).
        self._stream_end_mono: dict[Source, Optional[float]] = {"mic": None, "system": None}
        # Monotonic time of sample 0 of ``session_pcm[src]`` once a session
        # opens. Equal to ``now`` at ``open_session_on_arm`` time.
        self._session_t0_mono: float = 0.0

    # ---- helpers -----------------------------------------------------------

    def _gap_tolerance(self, source: Source) -> float:
        if source == "mic":
            return self.cfg.gap_tolerance_secs_mic
        return self.cfg.gap_tolerance_secs_system

    # ---- session lifecycle -------------------------------------------------

    def open_session_on_arm(
        self,
        now: float,
        *,
        arm_app_key: Optional[str] = None,
        arm_app_display: Optional[str] = None,
    ) -> None:
        """Open a session immediately at arm time, with empty buffers.

        Production entrypoint: ``ArmController._arm_internal`` calls this
        right after ``reset_per_source_streams`` and before opening capture
        streams. ``session_t0_mono`` is anchored to ``now`` (the arm
        timestamp); subsequent frames flow directly into ``mic_pcm`` /
        ``sys_pcm`` via ``on_frame`` with gap-fill bridging the small
        delay between arm and the first real frame.

        ``arm_app_key`` is stashed on the session buffer so app.py can
        build a placeholder title that names the arm app when known.
        ``arm_app_display`` is the user-facing version of the same (from
        the matched ``DetectorSpec.display_name``); the sink prefers it
        for both placeholder + insight-card source-anchor labels because
        ``app_key`` is a stable lowercase ID with no human-friendly casing.

        Idempotent for repeated calls while OPEN — does nothing.
        """
        if self.state != SessionState.IDLE:
            return
        self._open_session(
            now, trigger=None, trigger_start_ts=0.0, t0_mono=now,
            arm_app_key=arm_app_key,
            arm_app_display=arm_app_display,
        )

    def _open_session(
        self,
        now: float,
        trigger: Optional[Source],
        trigger_start_ts: float,
        t0_mono: Optional[float] = None,
        arm_app_key: Optional[str] = None,
        arm_app_display: Optional[str] = None,
    ) -> None:
        self.state = SessionState.OPEN
        self._buffers = SessionBuffers(
            started_at=datetime.now(timezone.utc),
            started_monotonic=now,
            arm_app_key=arm_app_key,
            arm_app_display=arm_app_display,
        )
        self._session_start_mono = now
        self._last_voiced_mono["mic"] = now if trigger == "mic" else 0.0
        self._last_voiced_mono["system"] = now if trigger == "system" else 0.0
        # Session origin in monotonic time. Arm-triggered: anchored to ``now``
        # (the arm timestamp). VAD-triggered (legacy / test path): anchored
        # to the segment's mono time, which VAD already emits directly.
        if t0_mono is not None:
            session_t0 = t0_mono
        else:
            assert trigger is not None
            session_t0 = trigger_start_ts
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
        sample. Three regimes:

        * ``capture_mono_ts < session_t0_mono - jitter`` — stale frame from
          a previous arm cycle that leaked through the producer queue.
          Dropped entirely; appending it would inject pre-arm audio.
        * ``0 ≤ gap ≤ max_gap_fill_secs`` — legitimate latency (cold-start
          delay before the capture thread delivers its first frame, USB
          jitter, scheduler hiccup, etc.). Zero-filled so sample N of the
          buffer maps to ``session_t0_mono + N/sample_rate`` regardless of
          which source delivered first.
        * ``gap > max_gap_fill_secs`` — pathological (system suspend, USB
          reconnect after minutes). Re-anchored with a small audible
          discontinuity rather than injecting minutes of silence.
        """
        if (
            self.state not in (SessionState.OPEN, SessionState.PENDING_CLOSE)
            or self._buffers is None
        ):
            return

        # Drop stale frames left over from a previous arm cycle: anything
        # whose timestamp predates the current session_t0 (beyond the
        # arm-boundary jitter tolerance) is from before we opened. Without
        # this guard, a frame with `capture_mono_ts < session_t0_mono` would
        # produce a NEGATIVE gap (no zero-fill triggered) and silently
        # append pre-arm audio at the start of the session.
        if capture_mono_ts < self._session_t0_mono - _STALE_FRAME_JITTER_SECS:
            log.debug(
                "[session] %s: dropping stale frame (ts=%.2f < t0=%.2f)",
                source, capture_mono_ts, self._session_t0_mono,
            )
            return

        # int16 little-endian for compact buffering
        pcm16 = np.clip(frame * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        frame_samples = len(frame)
        frame_duration = frame_samples / self.sample_rate

        dst = self._buffers.mic_pcm if source == "mic" else self._buffers.sys_pcm
        stream_end = self._stream_end_mono[source]
        if stream_end is None:
            # Legacy test path (open_session_on_arm not called). Anchor to
            # capture_mono_ts so the first frame appends with gap=0; this
            # is the pre-v3.6 behavior for tests that drive on_frame without
            # going through open_session_on_arm.
            stream_end = capture_mono_ts

        gap_secs = capture_mono_ts - stream_end
        gap_tolerance = self._gap_tolerance(source)
        max_fill = self.cfg.max_gap_fill_secs
        if gap_secs > max_fill:
            # Pathological gap (> max_gap_fill_secs, default 30 s) — system
            # suspend, USB reconnect after a long stall, mono clock skip.
            # Re-anchor instead of zero-filling minutes of silence. Will
            # produce a small audible discontinuity and a one-time mic↔sys
            # misalignment up to the gap size, both of which are strictly
            # better than injecting tens of seconds of zeros.
            log.warning(
                "[session] %s: gap %.1fs exceeds cap %.1fs — re-anchoring",
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

        ``seg.start_ts`` / ``seg.end_ts`` arrive as monotonic seconds
        (VAD anchors them to the frame timestamps passed into ``feed``).
        We rebase to session-relative by subtracting ``_session_t0_mono``.

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
        rebased = SpeechSegment(
            source=seg.source,
            start_ts=max(0.0, seg.start_ts - self._session_t0_mono),
            end_ts=max(0.0, seg.end_ts - self._session_t0_mono),
        )
        if seg.source == "mic":
            self._buffers.mic_segments.append(rebased)
        else:
            self._buffers.sys_segments.append(rebased)
        self._last_voiced_mono[seg.source] = now
        log.debug(
            "[vad] %s speech %.2f→%.2f (%.1fs)",
            seg.source,
            rebased.start_ts,
            rebased.end_ts,
            rebased.duration,
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

    def reset_per_source_streams(self) -> None:
        """Reset per-source stream-end anchors so the next frame behaves as
        if the source started fresh. Called by the ArmController on every
        disarm → arm transition, alongside ``SileroVAD.reset()``.

        Must only be called while IDLE.
        """
        if self.state != SessionState.IDLE:
            raise RuntimeError(
                f"reset_per_source_streams requires state=IDLE, got {self.state}"
            )
        self._stream_end_mono = {"mic": None, "system": None}

    # ---- output API --------------------------------------------------------

    def take_closed_session(self) -> Optional[SessionBuffers]:
        if not self._closed_queue:
            return None
        return self._closed_queue.pop(0)


def evaluate_user_turn_gate(buffers: SessionBuffers, cfg: ConversationConfig) -> GateResult:
    """Cheap pre-upload gate: substantive user speech AND counterparty present.

    Single-threshold rule (v3.5.2+): cumulative mic voiced time across the
    whole session must be ≥ ``cfg.min_user_total_secs``, however distributed.
    One long turn, many short turns, doesn't matter.
    """
    mic_total = buffers.mic_total_voiced()
    mic_max = buffers.mic_max_turn()
    mic_turns = buffers.mic_turn_count()
    sys_total = buffers.sys_total_voiced()

    user_ok = mic_total >= cfg.min_user_total_secs
    sys_ok = sys_total >= cfg.min_sys_voiced_secs

    if not user_ok:
        reason = (
            f"FAIL substantive-user-turn (mic_total={mic_total:.1f}s "
            f"over {mic_turns} turns < {cfg.min_user_total_secs:.0f}s)"
        )
    elif not sys_ok:
        reason = (
            f"FAIL counterparty (sys_total={sys_total:.1f}s < {cfg.min_sys_voiced_secs:.1f}s — "
            f"no other side)"
        )
    else:
        reason = (
            f"PASS substantive-user-turn (mic_total={mic_total:.1f}s "
            f"over {mic_turns} turns ≥ {cfg.min_user_total_secs:.0f}s)"
        )

    return GateResult(
        passed=user_ok and sys_ok,
        reason=reason,
        mic_total=mic_total,
        mic_max_turn=mic_max,
        mic_turn_count=mic_turns,
        sys_total=sys_total,
    )


