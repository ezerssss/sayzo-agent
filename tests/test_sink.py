"""Round-trip serialization test for ConversationRecord."""
from __future__ import annotations

from datetime import datetime, timezone

from sayzo_agent.models import ConversationRecord
from sayzo_agent.retry import STATUS_PENDING, empty_upload_state
from sayzo_agent.sink import (
    CaptureSink,
    deserialize_record,
    local_clock_label,
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


def test_local_clock_label_formats_12hour_lowercase():
    # Naive datetime path — strftime + strip + lower.
    # 14:30 → "2:30 pm"; 09:05 → "9:05 am"; 00:15 → "12:15 am" (12-hour wraps).
    assert local_clock_label(datetime(2026, 5, 28, 14, 30)) == "2:30 pm"
    assert local_clock_label(datetime(2026, 5, 28, 9, 5)) == "9:05 am"
    assert local_clock_label(datetime(2026, 5, 28, 0, 15)) == "12:15 am"


def test_capture_sink_write_caches_local_clock_label(tmp_path, monkeypatch):
    """CaptureSink.write must persist the local-clock label into metadata
    so the post-capture insight chip uses the TZ from CAPTURE time, not
    from the (potentially-changed) OS TZ at fire time.

    Stubs encode_opus_stereo because we don't need real audio I/O — only
    that record.json carries metadata.local_clock_label.
    """
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()

    import sayzo_agent.sink as sink_mod
    monkeypatch.setattr(sink_mod, "encode_opus_stereo",
                        lambda *a, **k: None)

    sink = CaptureSink(captures_dir)
    started = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc)
    record = sink.write(
        arm_app_key="zoom",
        started_at=started,
        ended_at=ended,
        mic_pcm16=b"", sys_pcm16=b"",
        metadata={"close_reason": "joint_silence"},
        rec_id="rec_clock",
        arm_app_display="Zoom",
    )
    assert "local_clock_label" in record.metadata
    # Don't pin the wall-clock string — it depends on the test runner's
    # TZ (CI runs UTC; dev machines vary). Assert shape: a non-empty
    # string ending in "am" or "pm" so we catch a regression that drops
    # the lowercase or the am/pm token.
    label = record.metadata["local_clock_label"]
    assert isinstance(label, str) and label
    assert label.endswith(" am") or label.endswith(" pm")
    # Round-trips through record.json.
    from sayzo_agent.sink import read_record_from_dir
    rec_back = read_record_from_dir(captures_dir / "rec_clock")
    assert rec_back.metadata["local_clock_label"] == label


def test_capture_sink_write_respects_existing_local_clock_label(tmp_path, monkeypatch):
    """When metadata already carries a local_clock_label (e.g. callers
    pre-computed it), CaptureSink.write must NOT overwrite it."""
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    import sayzo_agent.sink as sink_mod
    monkeypatch.setattr(sink_mod, "encode_opus_stereo",
                        lambda *a, **k: None)

    sink = CaptureSink(captures_dir)
    started = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc)
    record = sink.write(
        arm_app_key=None,
        started_at=started,
        ended_at=ended,
        mic_pcm16=b"", sys_pcm16=b"",
        metadata={"local_clock_label": "9:00 am"},
        rec_id="rec_preset",
    )
    assert record.metadata["local_clock_label"] == "9:00 am"


def test_capture_sink_write_persists_arm_app_identity(tmp_path, monkeypatch):
    """The post-capture insight chip derives source-anchor from agent-side
    arm metadata (arm_app_key + arm_app_display), NOT from record.title.
    Sink must persist both at write time so the chip's wording is
    deterministic regardless of whether the server's later title-pass
    succeeds."""
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    import sayzo_agent.sink as sink_mod
    monkeypatch.setattr(sink_mod, "encode_opus_stereo",
                        lambda *a, **k: None)

    sink = CaptureSink(captures_dir)
    started = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc)
    record = sink.write(
        arm_app_key="teams_desktop",
        started_at=started,
        ended_at=ended,
        mic_pcm16=b"", sys_pcm16=b"",
        metadata={},
        rec_id="rec_teams",
        arm_app_display="Microsoft Teams",
    )
    assert record.metadata["arm_app_key"] == "teams_desktop"
    assert record.metadata["arm_app_display"] == "Microsoft Teams"
    # Placeholder title now uses the display_name too — so Settings → Captures
    # shows "Microsoft Teams call · ..." instead of "Teams_Desktop call · ...".
    assert record.title.startswith("Microsoft Teams call · ")


def test_capture_sink_write_hotkey_arm_omits_app_metadata(tmp_path, monkeypatch):
    """Hotkey arms have no app attribution — neither arm_app_key nor
    arm_app_display should be persisted (None / missing keys), so
    _source_label falls back to the "conversation" hotkey path."""
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    import sayzo_agent.sink as sink_mod
    monkeypatch.setattr(sink_mod, "encode_opus_stereo",
                        lambda *a, **k: None)

    sink = CaptureSink(captures_dir)
    started = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc)
    record = sink.write(
        arm_app_key=None,
        started_at=started,
        ended_at=ended,
        mic_pcm16=b"", sys_pcm16=b"",
        metadata={},
        rec_id="rec_hotkey",
    )
    assert "arm_app_key" not in record.metadata
    assert "arm_app_display" not in record.metadata
    assert record.title.startswith("Conversation · ")
