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
    """Per-source PCM buffers and VAD timelines for one open session."""

    mic_pcm: bytearray = field(default_factory=bytearray)
    sys_pcm: bytearray = field(default_factory=bytearray)
    mic_segments: list[SpeechSegment] = field(default_factory=list)
    sys_segments: list[SpeechSegment] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_monotonic: float = 0.0
    ended_monotonic: float = 0.0
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
