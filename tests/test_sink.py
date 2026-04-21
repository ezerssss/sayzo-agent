"""Round-trip serialization test for ConversationRecord."""
from __future__ import annotations

from datetime import datetime

from sayzo_agent.models import ConversationRecord, TranscriptLine
from sayzo_agent.retry import STATUS_PENDING, empty_upload_state
from sayzo_agent.sink import deserialize_record, serialize_record


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


def test_metadata_upload_roundtrip():
    """The new metadata.upload dict must survive serialize/deserialize."""
    rec = ConversationRecord(
        id="xyz789",
        started_at=datetime(2026, 4, 21, 12, 0, 0),
        ended_at=datetime(2026, 4, 21, 12, 5, 0),
        transcript=[],
        title="T",
        summary="S",
        audio_path="audio.opus",
        relevant_span=(0.0, 1.0),
        metadata={"close_reason": "joint_silence", "upload": empty_upload_state()},
    )
    restored = deserialize_record(serialize_record(rec))
    assert restored.metadata["upload"]["status"] == STATUS_PENDING
    assert restored.metadata["upload"]["attempts"] == 0
    assert restored.metadata["close_reason"] == "joint_silence"


def test_old_record_without_upload_metadata_still_deserializes():
    """Records written before this change had no metadata.upload key —
    deserialize must still work and metadata comes through unchanged."""
    legacy_data = {
        "id": "old1",
        "started_at": "2026-01-01T10:00:00",
        "ended_at": "2026-01-01T10:05:00",
        "title": "Legacy capture",
        "summary": "Before the upload-state feature.",
        "transcript": [],
        "audio_path": "audio.opus",
        "relevant_span": [0.0, 2.0],
        "metadata": {"close_reason": "joint_silence"},  # no "upload" key
    }
    restored = deserialize_record(legacy_data)
    assert restored.id == "old1"
    assert "upload" not in restored.metadata
    assert restored.metadata["close_reason"] == "joint_silence"
