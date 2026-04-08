"""Silence-bounded session detector and substantive-user-turn gate.

This module is intentionally pure (no audio I/O, no model loading) so it can
be unit-tested with synthetic VAD events.
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
        - on_frame(source, frame): raw PCM written to the active session buffer
          if a session is open. Frames *before* a session opens are discarded.
        - on_segment(seg): a SpeechSegment closed by the VAD on `source`. This
          opens a session if idle, refreshes the joint-silence timer, and
          appends to the session's segment list.
        - tick(now_monotonic): called periodically to check for session close
          via sustained joint silence or safety cap.

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
        # Rolling pre-session PCM buffer (see ConversationConfig.max_pre_buffer_secs).
        # Indexed by "samples since last VAD reset" — same clock the VAD uses
        # for segment timestamps, so seg.start_ts maps to a byte offset
        # directly. _pre_start_sample tracks how many samples we've dropped
        # off the front of the buffer due to the cap.
        self._pre_buffers: dict[Source, bytearray] = {
            "mic": bytearray(),
            "system": bytearray(),
        }
        self._pre_start_sample: dict[Source, int] = {"mic": 0, "system": 0}
        self._max_pre_buffer_bytes = int(config.max_pre_buffer_secs * sample_rate) * 2
        # Offset subtracted from incoming VAD segment timestamps while a
        # session is open. Set when a session opens to the triggering
        # segment's start_ts, so the session timeline (and PCM buffer) both
        # start at 0 regardless of where the VAD clock was.
        self._session_ts_offset: float = 0.0

    # ---- session lifecycle -------------------------------------------------

    def _open_session(self, now: float, trigger: Source, trigger_start_ts: float) -> None:
        self.state = SessionState.OPEN
        self._buffers = SessionBuffers(
            started_at=datetime.now(timezone.utc),
            started_monotonic=now,
        )
        self._session_start_mono = now
        self._last_voiced_mono["mic"] = now if trigger == "mic" else 0.0
        self._last_voiced_mono["system"] = now if trigger == "system" else 0.0
        self._session_ts_offset = trigger_start_ts

        # Backfill from the pre-buffers so mic_pcm[0] / sys_pcm[0] correspond
        # to trigger_start_ts on the VAD clock (i.e. the moment the triggering
        # speech actually began) rather than "now" (which is post-hangover).
        trigger_start_sample = int(trigger_start_ts * self.sample_rate)
        backfilled = {"mic": 0, "system": 0}
        for src in ("mic", "system"):
            pre = self._pre_buffers[src]
            if not pre:
                continue
            # Where does trigger_start_sample land inside the pre-buffer?
            local_start_sample = trigger_start_sample - self._pre_start_sample[src]
            local_start_sample = max(0, local_start_sample)
            start_byte = local_start_sample * 2
            if start_byte < len(pre):
                chunk = bytes(pre[start_byte:])
                if src == "mic":
                    self._buffers.mic_pcm.extend(chunk)
                else:
                    self._buffers.sys_pcm.extend(chunk)
                backfilled[src] = len(chunk) // 2
        # Pre-buffers are consumed — clear and reset so the next IDLE period
        # starts clean.
        for src in ("mic", "system"):
            self._pre_buffers[src].clear()
            self._pre_start_sample[src] = 0

        log.info(
            "[session] OPENED at +0.00s (trigger=%s, backfilled mic=%.2fs sys=%.2fs)",
            trigger,
            backfilled["mic"] / self.sample_rate,
            backfilled["system"] / self.sample_rate,
        )

    def _close_session(self, now: float, reason: SessionCloseReason) -> None:
        if self._buffers is None:
            return
        self._buffers.ended_monotonic = now
        self._buffers.close_reason = reason
        elapsed = self._buffers.elapsed()
        log.info(
            "[session] CLOSED after %.1fs reason=%s mic_total=%.1fs (max_turn=%.1fs over %d turns) sys_total=%.1fs",
            elapsed,
            reason.value,
            self._buffers.mic_total_voiced(),
            self._buffers.mic_max_turn(),
            self._buffers.mic_turn_count(),
            self._buffers.sys_total_voiced(),
        )
        self._closed_queue.append(self._buffers)
        self._buffers = None
        self.state = SessionState.IDLE
        self._last_voiced_mono = {"mic": 0.0, "system": 0.0}
        self._session_ts_offset = 0.0
        # Clear pre-buffers so the next IDLE period starts in sync with the
        # VAD reset that app.py performs right after this.
        for src in ("mic", "system"):
            self._pre_buffers[src].clear()
            self._pre_start_sample[src] = 0

    # ---- input API ---------------------------------------------------------

    def on_frame(self, source: Source, frame: np.ndarray, now: float) -> None:
        """Append raw PCM to the active session buffer, or to the pre-buffer
        when idle so the opening turn can be backfilled later."""
        # int16 little-endian for compact buffering
        pcm16 = np.clip(frame * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        if self.state != SessionState.OPEN or self._buffers is None:
            pre = self._pre_buffers[source]
            pre.extend(pcm16)
            if len(pre) > self._max_pre_buffer_bytes:
                overflow = len(pre) - self._max_pre_buffer_bytes
                del pre[:overflow]
                self._pre_start_sample[source] += overflow // 2
            return
        if source == "mic":
            self._buffers.mic_pcm.extend(pcm16)
        else:
            self._buffers.sys_pcm.extend(pcm16)

    def on_segment(self, seg: SpeechSegment, now: float) -> None:
        """Register a closed VAD segment on `seg.source`."""
        if self.state == SessionState.IDLE:
            self._open_session(now, seg.source, seg.start_ts)
            assert self._buffers is not None
        assert self._buffers is not None
        # Rebase to the session timeline so seg.start_ts=0 corresponds to the
        # first sample of the backfilled session PCM buffer.
        rebased = SpeechSegment(
            source=seg.source,
            start_ts=max(0.0, seg.start_ts - self._session_ts_offset),
            end_ts=max(0.0, seg.end_ts - self._session_ts_offset),
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
