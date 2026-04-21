"""Core dataclasses passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional


Source = Literal["mic", "system"]


class SessionCloseReason(str, Enum):
    JOINT_SILENCE = "joint_silence"
    SAFETY_CAP = "safety_cap"
    SHUTDOWN = "shutdown"


@dataclass
class SpeechSegment:
    """A contiguous voiced region detected by the VAD on one source."""

    source: Source
    start_ts: float  # seconds, monotonic from session start
    end_ts: float
    # Raw PCM is held in the session buffer, not duplicated here.

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


@dataclass
class SessionBuffers:
    """Per-source PCM buffers and VAD timelines for one open session.

    After the detector closes a session, `mic_pcm` and `sys_pcm` are the same
    length and both start at `session_t0_mono` in `time.monotonic()` seconds:
    `mic_pcm[sample_N]` and `sys_pcm[sample_N]` correspond to the same
    wall-clock moment. All `SpeechSegment.start_ts` / `end_ts` and all
    Whisper-derived timestamps after STT are in session seconds (= seconds
    from `session_t0_mono`) so they index either PCM buffer directly.
    """

    mic_pcm: bytearray = field(default_factory=bytearray)
    sys_pcm: bytearray = field(default_factory=bytearray)
    mic_segments: list[SpeechSegment] = field(default_factory=list)
    sys_segments: list[SpeechSegment] = field(default_factory=list)
    # Mic VAD segments that the echo guard classified as speaker-to-mic bleed
    # and removed from `mic_segments`. Preserved so downstream steps can zero
    # the corresponding mic PCM before STT and include the spans in
    # record.json metadata / debug dumps. Empty when echo guard is disabled
    # or found nothing to drop.
    mic_echo_segments: list[SpeechSegment] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_monotonic: float = 0.0
    ended_monotonic: float = 0.0
    # Monotonic time of mic_pcm[0] = sys_pcm[0]. Set when the session opens
    # and the backfill alignment lands both channels at the same wall-clock
    # moment. Differs from `started_monotonic` (which is the "_open_session
    # was called" moment) because backfill extends the audio earlier.
    session_t0_mono: float = 0.0
    session_end_mono: float = 0.0
    close_reason: Optional[SessionCloseReason] = None

    def mic_total_voiced(self) -> float:
        return sum(s.duration for s in self.mic_segments)

    def sys_total_voiced(self) -> float:
        return sum(s.duration for s in self.sys_segments)

    def mic_max_turn(self) -> float:
        return max((s.duration for s in self.mic_segments), default=0.0)

    def mic_turn_count(self) -> int:
        return len(self.mic_segments)

    def elapsed(self) -> float:
        return self.ended_monotonic - self.started_monotonic

    def pcm_duration(self, sample_rate: int = 16000) -> float:
        """Duration of the saved audio in seconds. After close, this equals
        `session_end_mono - session_t0_mono` modulo rounding."""
        return max(len(self.mic_pcm), len(self.sys_pcm)) / 2 / sample_rate


@dataclass
class TranscriptLine:
    speaker: str  # "user", "other_1", "other_2", ...
    start: float
    end: float
    text: str


@dataclass
class ConversationRecord:
    id: str
    started_at: datetime
    ended_at: datetime
    transcript: list[TranscriptLine]
    title: str
    summary: str
    audio_path: str  # relative to capture directory
    relevant_span: tuple[float, float]
    metadata: dict = field(default_factory=dict)
