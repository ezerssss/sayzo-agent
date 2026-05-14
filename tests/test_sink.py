"""Round-trip serialization test for ConversationRecord."""
from __future__ import annotations

from datetime import datetime

from sayzo_agent.models import ConversationRecord
from sayzo_agent.retry import STATUS_PENDING, empty_upload_state
from sayzo_agent.sink import (
    deserialize_record,
    serialize_record,
    serialize_record_for_upload,
)


def test_record_roundtrip():
    rec = ConversationRecord(
        id="abc123",
        started_at=datetime(2026, 4, 8, 10, 0, 0),
        ended_at=datetime(2026, 4, 8, 10, 5, 0),
        title="Quick greeting",
        summary="Greeting exchange.",
        metadata={"close_reason": "joint_silence"},
    )
    data = serialize_record(rec)
    restored = deserialize_record(data)
    assert restored.id == rec.id
    assert restored.started_at == rec.started_at
    assert restored.ended_at == rec.ended_at
    assert restored.title == rec.title
    assert restored.summary == rec.summary
    assert restored.metadata == rec.metadata


def test_metadata_upload_roundtrip():
    """The new metadata.upload dict must survive serialize/deserialize."""
    rec = ConversationRecord(
        id="xyz789",
        started_at=datetime(2026, 4, 21, 12, 0, 0),
        ended_at=datetime(2026, 4, 21, 12, 5, 0),
        title="T",
        summary="S",
        metadata={"close_reason": "joint_silence", "upload": empty_upload_state()},
    )
    restored = deserialize_record(serialize_record(rec))
    assert restored.metadata["upload"]["status"] == STATUS_PENDING
    assert restored.metadata["upload"]["attempts"] == 0
    assert restored.metadata["close_reason"] == "joint_silence"


def test_legacy_record_with_v2_fields_still_deserializes():
    """Pre-3.0 records on disk carry transcript / audio_path / relevant_span /
    metadata.local_llm_used / metadata.placeholder_title. The new
    deserializer must drop them silently — the dataclass no longer accepts
    those fields."""
    legacy_data = {
        "id": "old1",
        "started_at": "2026-01-01T10:00:00",
        "ended_at": "2026-01-01T10:05:00",
        "title": "Legacy capture",
        "summary": "From a pre-3.0 agent.",
        "transcript": [
            {"speaker": "user", "start": 0.0, "end": 2.5, "text": "Hello there."},
            {"speaker": "other_1", "start": 2.7, "end": 4.0, "text": "Hi!"},
        ],
        "audio_path": "audio.opus",
        "relevant_span": [0.0, 2.0],
        "metadata": {
            "close_reason": "joint_silence",
            "local_llm_used": False,
            "placeholder_title": True,
        },
    }
    restored = deserialize_record(legacy_data)
    assert restored.id == "old1"
    assert restored.title == "Legacy capture"
    assert restored.summary == "From a pre-3.0 agent."
    # Stripped fields are NOT exposed on the new dataclass.
    assert not hasattr(restored, "transcript")
    assert not hasattr(restored, "audio_path")
    assert not hasattr(restored, "relevant_span")
    # Legacy metadata flags pass through untouched (deserializer doesn't
    # filter metadata — only the top-level keys).
    assert restored.metadata["close_reason"] == "joint_silence"
    assert restored.metadata["local_llm_used"] is False
    assert restored.metadata["placeholder_title"] is True


def test_serialize_record_for_upload_strips_local_only_fields():
    """The upload-only serializer must NOT send title/summary or local-only
    metadata (upload state, echo_guard report) to the server."""
    rec = ConversationRecord(
        id="upload1",
        started_at=datetime(2026, 5, 14, 14, 32, 1),
        ended_at=datetime(2026, 5, 14, 14, 47, 18),
        title="Conversation · 2026-05-14 14:32",  # local placeholder
        summary="",
        metadata={
            "close_reason": "joint_silence",
            "upload": empty_upload_state(),
            "echo_guard": {"enabled": True, "segments_kept": 4},
        },
    )
    payload = serialize_record_for_upload(rec)
    assert set(payload.keys()) == {"id", "started_at", "ended_at", "metadata"}
    assert payload["id"] == "upload1"
    assert payload["started_at"] == "2026-05-14T14:32:01"
    assert payload["ended_at"] == "2026-05-14T14:47:18"
    # Only close_reason flows to the server.
    assert payload["metadata"] == {"close_reason": "joint_silence"}
    # Title/summary NOT sent.
    assert "title" not in payload
    assert "summary" not in payload
    # Legacy fields NOT sent.
    assert "transcript" not in payload
    assert "audio_path" not in payload
    assert "relevant_span" not in payload
