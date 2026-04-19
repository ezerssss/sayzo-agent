"""Silence-bounded session detector and substantive-user-turn gate.

This module is intentionally pure (no audio I/O, no model loading) so it can
be unit-tested with synthetic VAD events.

Mono-clock invariant
--------------------
Every frame passed to ``on_frame`` carries a ``capture_mono_ts`` — the
``time.monotonic()`` value corresponding to the first sample in the frame
(ideally stamped at the hardware ADC). The detector uses these stamps to
keep both source buffers aligned to a single session timeline:

* Per source, sample ``N`` of the pre- or session-buffer corresponds to
  monotonic time ``t0_mono + N/sample_rate``. Dropped / late frames are
  zero-filled to preserve this invariant.
* At session open, both ``mic_pcm[0]`` and ``sys_pcm[0]`` are aligned to a
  single ``session_t0_mono`` (via slicing the pre-buffer or zero-padding the
  front). After that, ``mic_pcm[N]`` and ``sys_pcm[N]`` correspond to the
  same wall-clock moment.
* At session close, the shorter of ``mic_pcm`` / ``sys_pcm`` is zero-padded
  at the tail so both buffers have equal length.

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
from typing import Optional

import numpy as np

from .config import ConversationConfig
from .models import SessionBuffers, SessionCloseReason, SpeechSegment, Source

log = logging.getLogger(__name__)


class SessionState(str, Enum):
    IDLE = "idle"
    OPEN = "open"


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
        # Track the last voiced wall time on each source (monotonic seconds).
        self._last_voiced_mono: dict[Source, float] = {"mic": 0.0, "system": 0.0}
        self._session_start_mono: float = 0.0
        # Rolling pre-session PCM buffer. Sample N of pre_buffer corresponds
        # to monotonic time `_pre_start_mono[src] + N/sample_rate` (with
        # ``_pre_start_mono[src]`` advancing when overflow drops samples off
        # the front).
        self._pre_buffers: dict[Source, bytearray] = {
            "mic": bytearray(),
            "system": bytearray(),
        }
        self._pre_start_sample: dict[Source, int] = {"mic": 0, "system": 0}
        self._max_pre_buffer_bytes = int(config.max_pre_buffer_secs * sample_rate) * 2
        # Monotonic-clock anchoring for each source. Set on the SECOND frame
        # received (the first frame often carries device-priming stall); never
        # modified afterward. Used to convert VAD sample indices (which are in
        # "real samples since this source started" units) into absolute
        # monotonic seconds: `mono(vad_sample) = _source_epoch_mono[src] +
        # vad_sample / sample_rate`. Also anchors the pre-buffer mono
        # timeline: `mono(pre_sample_N) = _source_epoch_mono[src] +
        # (_pre_start_sample[src] + N) / sample_rate`.
        self._source_epoch_mono: dict[Source, Optional[float]] = {"mic": None, "system": None}
        self._source_frames_seen: dict[Source, int] = {"mic": 0, "system": 0}
        # Mono time of the end of each source's current stream (pre-buffer
        # tail while idle, session-buffer tail while open). Advances by
        # frame_duration on each append and by gap on each zero-fill.
        self._stream_end_mono: dict[Source, Optional[float]] = {"mic": None, "system": None}
        # Monotonic time of sample 0 of ``session_pcm[src]`` once a session
        # opens. Shared between mic and sys (by construction — backfill
        # aligns both).
        self._session_t0_mono: float = 0.0
        # Legacy VAD-sample-second offset used as a fallback when a source
        # has no epoch yet (test path). Matches the pre-mono-clock behavior:
        # segment timestamps are rebased by subtracting the triggering
        # segment's ``start_ts``.
        self._session_ts_offset: float = 0.0

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

    def _open_session(
        self,
        now: float,
        trigger: Source,
        trigger_start_ts: float,
    ) -> None:
        self.state = SessionState.OPEN
        self._buffers = SessionBuffers(
            started_at=datetime.now(timezone.utc),
            started_monotonic=now,
        )
        self._session_start_mono = now
        self._last_voiced_mono["mic"] = now if trigger == "mic" else 0.0
        self._last_voiced_mono["system"] = now if trigger == "system" else 0.0
        # Legacy fallback offset — used when a source has no mono epoch (tests
        # pass segments directly without frames).
        self._session_ts_offset = trigger_start_ts
        # True session origin in monotonic time. Falls back to
        # ``trigger_start_ts`` in the tests-without-frames path, which keeps
        # `_session_time` consistent with the legacy rebase behavior.
        trigger_mono = self._seg_mono(trigger, trigger_start_ts)
        self._session_t0_mono = trigger_mono
        self._buffers.session_t0_mono = trigger_mono

        # Backfill mic_pcm and sys_pcm so their sample 0 both correspond to
        # `session_t0_mono` in monotonic time. Since each source's pre-buffer
        # is indexed against its own epoch, we compute the pre-buffer's
        # mono-time range per source and either slice (pre covers
        # session_t0_mono) or zero-pad the front (pre starts after
        # session_t0_mono, e.g., because this source lagged in starting up).
        backfilled: dict[Source, int] = {"mic": 0, "system": 0}
        padded: dict[Source, int] = {"mic": 0, "system": 0}
        for src in ("mic", "system"):
            pre = self._pre_buffers[src]
            epoch = self._source_epoch_mono[src]
            # Fallback: assume VAD clock == shared clock (test path).
            # ``_seg_mono`` also uses this fallback, so session_t0_mono and
            # pre_*_mono end up in the same fallback timeline.
            eff_epoch = 0.0 if epoch is None else epoch
            pre_samples = len(pre) // 2
            pre_start_mono = eff_epoch + self._pre_start_sample[src] / self.sample_rate
            pre_end_mono = pre_start_mono + pre_samples / self.sample_rate

            dst = self._buffers.mic_pcm if src == "mic" else self._buffers.sys_pcm

            if pre_samples == 0 or trigger_mono >= pre_end_mono:
                # No usable pre-buffer data for this source — session_pcm[src]
                # starts empty. The next real frame on this source will be
                # gap-filled via the on_frame path to align with
                # session_t0_mono.
                self._stream_end_mono[src] = trigger_mono
                continue

            if trigger_mono <= pre_start_mono:
                # Session origin is earlier than the pre-buffer's oldest
                # sample. Zero-pad the front, then copy the whole pre-buffer.
                pad_samples = int(round((pre_start_mono - trigger_mono) * self.sample_rate))
                if pad_samples > 0:
                    dst.extend(b"\x00\x00" * pad_samples)
                    padded[src] = pad_samples
                dst.extend(bytes(pre))
                backfilled[src] = pre_samples
                self._stream_end_mono[src] = pre_end_mono
            else:
                # Session origin lands inside the pre-buffer. Slice from that
                # sample onward.
                local_start_sample = int(round((trigger_mono - pre_start_mono) * self.sample_rate))
                local_start_sample = max(0, min(pre_samples, local_start_sample))
                start_byte = local_start_sample * 2
                if start_byte < len(pre):
                    chunk = bytes(pre[start_byte:])
                    dst.extend(chunk)
                    backfilled[src] = len(chunk) // 2
                self._stream_end_mono[src] = pre_end_mono

        # Pre-buffers are consumed — clear and reset so the next IDLE period
        # starts clean. Source-level counters (`_source_epoch_mono`,
        # `_source_frames_seen`) persist across sessions; that's what lets
        # VAD segment timestamps (which grow unbounded across sessions) keep
        # converting to monotonic time correctly.
        for src in ("mic", "system"):
            self._pre_buffers[src].clear()
            self._pre_start_sample[src] = 0

        log.info(
            "[session] OPENED at +0.00s (trigger=%s, backfilled mic=%.2fs sys=%.2fs, "
            "front-pad mic=%.2fs sys=%.2fs)",
            trigger,
            backfilled["mic"] / self.sample_rate,
            backfilled["system"] / self.sample_rate,
            padded["mic"] / self.sample_rate,
            padded["system"] / self.sample_rate,
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
        self._session_ts_offset = 0.0
        self._session_t0_mono = 0.0
        # Source-level stream_end_mono is no longer meaningful post-close —
        # the next idle period rebuilds the pre-buffer from scratch.
        self._stream_end_mono = {"mic": None, "system": None}
        # Clear pre-buffers so the next IDLE period starts in sync.
        for src in ("mic", "system"):
            self._pre_buffers[src].clear()
            self._pre_start_sample[src] = 0

    # ---- input API ---------------------------------------------------------

    def on_frame(
        self,
        source: Source,
        frame: np.ndarray,
        capture_mono_ts: float,
        now: float,
    ) -> None:
        """Append raw PCM to the active session buffer, or to the pre-buffer
        when idle so the opening turn can be backfilled later.

        ``capture_mono_ts`` is the monotonic time of the frame's first
        sample. It maintains the mono-clock invariant: when the actual
        arrival lags expectation (dropped / late frame), the gap is
        zero-filled so buffer offsets stay interchangeable with wall-clock
        offsets.
        """
        # int16 little-endian for compact buffering
        pcm16 = np.clip(frame * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        frame_samples = len(frame)
        frame_duration = frame_samples / self.sample_rate

        # Anchor epoch on the SECOND frame — the first frame after stream
        # .start() often carries device-priming jitter (PortAudio startup
        # buffer, pipe-read warmup). Skipping it costs ~one frame of
        # pre-buffer fidelity and buys a stable anchor.
        self._source_frames_seen[source] += 1
        if self._source_epoch_mono[source] is None and self._source_frames_seen[source] >= 2:
            # Anchor so that mono time of sample 0 (the very first frame we
            # ever received on this source) = capture_mono_ts of THIS
            # (second) frame minus the first frame's duration. Both frames
            # land coherently on the timeline, and subsequent VAD sample
            # counts convert correctly.
            first_frame_duration = frame_duration  # assume uniform
            self._source_epoch_mono[source] = capture_mono_ts - first_frame_duration

        # Determine which buffer we're writing to, and look up the current
        # "end of this source's stream" in monotonic time.
        if self.state != SessionState.OPEN or self._buffers is None:
            pre = self._pre_buffers[source]
            dst = pre
            stream_end = self._stream_end_mono[source]
            if stream_end is None:
                # First frame on this source — seed the timeline. The very
                # first frame's samples correspond to [capture_mono_ts,
                # capture_mono_ts + frame_duration].
                stream_end = capture_mono_ts
            pre_mode = True
        else:
            dst = self._buffers.mic_pcm if source == "mic" else self._buffers.sys_pcm
            stream_end = self._stream_end_mono[source]
            if stream_end is None:
                stream_end = capture_mono_ts
            pre_mode = False

        # Gap-fill: if the frame's capture time is meaningfully later than
        # the stream's current end, pad with zeros to preserve the sample-
        # to-mono-time invariant.
        gap_secs = capture_mono_ts - stream_end
        gap_tolerance = self._gap_tolerance(source)
        if gap_secs > gap_tolerance:
            gap_samples = int(round(gap_secs * self.sample_rate))
            if gap_samples > 0:
                dst.extend(b"\x00\x00" * gap_samples)
                stream_end = stream_end + gap_samples / self.sample_rate

        # Append the real frame's PCM.
        dst.extend(pcm16)
        stream_end = stream_end + frame_duration
        self._stream_end_mono[source] = stream_end

        if pre_mode:
            # Trim pre-buffer overflow. `_pre_start_sample` advances so the
            # pre-buffer's mono-time anchor still maps sample offsets to
            # correct monotonic times.
            if len(dst) > self._max_pre_buffer_bytes:
                overflow = len(dst) - self._max_pre_buffer_bytes
                del dst[:overflow]
                self._pre_start_sample[source] += overflow // 2

    def on_segment(self, seg: SpeechSegment, now: float) -> None:
        """Register a closed VAD segment on `seg.source`."""
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
        """Periodic check for session close."""
        if self.state != SessionState.OPEN or self._buffers is None:
            return

        # Safety cap
        if now - self._session_start_mono >= self.cfg.max_session_secs:
            self._close_session(now, SessionCloseReason.SAFETY_CAP)
            return

        # Joint silence: both sources have been quiet for >= threshold
        last_any = max(self._last_voiced_mono["mic"], self._last_voiced_mono["system"])
        if last_any > 0 and (now - last_any) >= self.cfg.joint_silence_close_secs:
            self._close_session(now, SessionCloseReason.JOINT_SILENCE)

    def force_close(self, now: float) -> None:
        if self.state == SessionState.OPEN:
            self._close_session(now, SessionCloseReason.SHUTDOWN)

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
