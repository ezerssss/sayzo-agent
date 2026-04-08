"""Round-trip serialization test for ConversationRecord."""
from __future__ import annotations

from datetime import datetime

from eloquy_agent.models import ConversationRecord, TranscriptLine
from eloquy_agent.sink import deserialize_record, serialize_record


def test_record_roundtrip():
    rec = ConversationRecord(
        id="abc123",
        started_at=datetime(2026, 4, 8, 10, 0, 0),
        ended_at=datetime(2026, 4, 8, 10, 5, 0),
        transcript=[
            TranscriptLine(speaker="user", start=0.0, end=2.5, text="Hello there."),
            TranscriptLine(speaker="other_1", start=2.7, end=4.0, text="Hi!"),
        ],
        title="Quick greeting",
        summary="Greeting exchange.",
        audio_path="audio.opus",
        relevant_span=(0.0, 4.0),
        metadata={"close_reason": "joint_silence"},
    )
    data = serialize_record(rec)
    restored = deserialize_record(data)
    assert restored.id == rec.id
    assert restored.started_at == rec.started_at
    assert restored.transcript == rec.transcript
    assert restored.title == rec.title
    assert restored.relevant_span == rec.relevant_span
    assert restored.metadata == rec.metadata
