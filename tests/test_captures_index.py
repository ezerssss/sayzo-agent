"""Pure-logic tests for sayzo_agent.captures_index.

No real captures, no audio, no IPC — just write tiny record.json files
into a tmp dir and assert the enumerator + status mapper handle every
shape we expect."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sayzo_agent.captures_index import (
    CaptureStatus,
    bucket_for,
    delete_capture,
    derive_status,
    enumerate_captures,
    friendly_label,
    is_valid_id,
    request_retry,
    summary_to_dict,
)
from sayzo_agent.retry import (
    STATUS_AUTH_BLOCKED,
    STATUS_CREDIT_BLOCKED,
    STATUS_FAILED_PERMANENT,
    STATUS_FAILED_TRANSIENT,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    STATUS_UPLOADED,
    empty_upload_state,
)


def _write_record(
    captures_dir: Path,
    rec_id: str,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    title: str = "Test",
    summary: str = "",
    metadata: dict | None = None,
    write_audio: bool = True,
) -> Path:
    rec_dir = captures_dir / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": rec_id,
        "started_at": started_at.isoformat(),
        "ended_at": (ended_at or started_at + timedelta(minutes=2)).isoformat(),
        "title": title,
        "summary": summary,
        "transcript": [],
        "audio_path": "audio.opus" if write_audio else "",
        "relevant_span": [0.0, 1.0],
        "metadata": metadata or {},
    }
    (rec_dir / "record.json").write_text(json.dumps(data), encoding="utf-8")
    if write_audio:
        (rec_dir / "audio.opus").write_bytes(b"\x00")
    return rec_dir


# ---------------------------------------------------------------------------
# derive_status: every retry status + dropped + missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        (STATUS_PENDING, CaptureStatus.PENDING),
        (STATUS_IN_FLIGHT, CaptureStatus.UPLOADING),
        (STATUS_UPLOADED, CaptureStatus.UPLOADED),
        (STATUS_FAILED_TRANSIENT, CaptureStatus.FAILED_TRANSIENT),
        (STATUS_FAILED_PERMANENT, CaptureStatus.FAILED_PERMANENT),
        (STATUS_CREDIT_BLOCKED, CaptureStatus.CREDIT_BLOCKED),
        (STATUS_AUTH_BLOCKED, CaptureStatus.AUTH_BLOCKED),
    ],
)
def test_derive_status_maps_every_retry_status(raw_status, expected):
    metadata = {"upload": dict(empty_upload_state(), status=raw_status)}
    assert derive_status(metadata) == expected


def test_derive_status_dropped_wins_over_upload():
    metadata = {"dropped": {"reason": "gate_failed"}, "upload": {"status": STATUS_PENDING}}
    assert derive_status(metadata) == CaptureStatus.DROPPED


def test_derive_status_missing_metadata_defaults_to_pending():
    assert derive_status(None) == CaptureStatus.PENDING
    assert derive_status({}) == CaptureStatus.PENDING


# ---------------------------------------------------------------------------
# bucket_for + friendly_label coverage
# ---------------------------------------------------------------------------


def test_bucket_assignment_for_every_status():
    assert bucket_for(CaptureStatus.PROCESSING) == "in_progress"
    assert bucket_for(CaptureStatus.PENDING) == "in_progress"
    assert bucket_for(CaptureStatus.UPLOADING) == "in_progress"
    assert bucket_for(CaptureStatus.UPLOADED) == "uploaded"
    assert bucket_for(CaptureStatus.FAILED_TRANSIENT) == "failed"
    assert bucket_for(CaptureStatus.FAILED_PERMANENT) == "failed"
    assert bucket_for(CaptureStatus.CREDIT_BLOCKED) == "in_progress"
    assert bucket_for(CaptureStatus.AUTH_BLOCKED) == "in_progress"
    assert bucket_for(CaptureStatus.DROPPED) == "skipped"


def test_friendly_label_returns_label_and_tone():
    label, tone = friendly_label(CaptureStatus.UPLOADED)
    assert label == "Saved to your account"
    assert tone == "green"
    label, tone = friendly_label(CaptureStatus.DROPPED, dropped_reason="gate_failed")
    assert "Skipped" in label
    assert tone == "gray"


# ---------------------------------------------------------------------------
# enumerate_captures: end-to-end with realistic mix
# ---------------------------------------------------------------------------


def test_enumerate_captures_end_to_end(tmp_path):
    base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1 uploaded record (oldest)
    _write_record(
        tmp_path,
        "111111111111",
        started_at=base - timedelta(hours=2),
        title="Standup",
        metadata={"upload": dict(empty_upload_state(), status=STATUS_UPLOADED)},
    )
    # 1 transient retry with attempts=2
    _write_record(
        tmp_path,
        "222222222222",
        started_at=base - timedelta(hours=1),
        title="Demo",
        metadata={
            "upload": dict(
                empty_upload_state(),
                status=STATUS_FAILED_TRANSIENT,
                attempts=2,
                last_error_message="Network error",
            ),
        },
    )
    # 1 dropped stub (no audio file)
    _write_record(
        tmp_path,
        "333333333333",
        started_at=base - timedelta(minutes=30),
        title="",
        metadata={"dropped": {"reason": "gate_failed", "reason_label": "Skipped"}},
        write_audio=False,
    )
    # 1 corrupt record.json — ignored silently
    bad = tmp_path / "444444444444"
    bad.mkdir()
    (bad / "record.json").write_text("{not valid json", encoding="utf-8")
    # 1 hidden file (mirrors .upload_state.json) — ignored
    (tmp_path / ".upload_state.json").write_text("{}", encoding="utf-8")

    # 1 in-progress synthetic entry
    processing = {
        "555555555555": {
            "label": "Sayzo is analyzing this",
            "started_at": base.isoformat(),
            "duration_secs": 14.2,
        }
    }

    summaries = enumerate_captures(tmp_path, processing)

    ids = [s.id for s in summaries]
    assert ids[0] == "555555555555", "processing row must come first"
    # The remaining rows are sorted most-recent first by started_at.
    rest = ids[1:]
    assert rest == ["333333333333", "222222222222", "111111111111"]

    by_id = {s.id: s for s in summaries}
    assert by_id["111111111111"].status == CaptureStatus.UPLOADED
    assert by_id["111111111111"].bucket == "uploaded"
    assert by_id["111111111111"].has_audio is True
    assert by_id["222222222222"].status == CaptureStatus.FAILED_TRANSIENT
    assert by_id["222222222222"].attempts == 2
    assert by_id["222222222222"].detail == "Network error"
    assert by_id["333333333333"].status == CaptureStatus.DROPPED
    assert by_id["333333333333"].bucket == "skipped"
    assert by_id["333333333333"].has_audio is False
    assert by_id["333333333333"].dropped_reason == "gate_failed"
    assert by_id["555555555555"].is_processing is True
    assert by_id["555555555555"].badge_tone == "blue"


def test_enumerate_captures_dedupes_processing_against_disk(tmp_path):
    """If the proc_id is already on disk (race between write and pop), the
    synthetic row should NOT show twice."""
    base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=base,
        metadata={"upload": dict(empty_upload_state(), status=STATUS_UPLOADED)},
    )
    processing = {"abcdef012345": {"label": "Sayzo is analyzing this", "started_at": base.isoformat(), "duration_secs": 0}}

    summaries = enumerate_captures(tmp_path, processing)
    matching = [s for s in summaries if s.id == "abcdef012345"]
    # The on-disk record wins; the synthetic processing row is suppressed.
    assert len(matching) == 1
    assert matching[0].status == CaptureStatus.UPLOADED


def test_enumerate_captures_handles_missing_directory(tmp_path):
    missing = tmp_path / "nope"
    assert enumerate_captures(missing, {}) == []


# ---------------------------------------------------------------------------
# delete_capture
# ---------------------------------------------------------------------------


def test_delete_capture_rejects_invalid_ids(tmp_path):
    # Path-traversal style inputs.
    for bad in ["..", "../foo", "/etc/passwd", "ABCDEF012345", "tooshort", "z" * 12]:
        with pytest.raises(ValueError):
            delete_capture(tmp_path, bad)


def test_delete_capture_removes_directory(tmp_path):
    rec_dir = _write_record(
        tmp_path,
        "abcdef012345",
        started_at=datetime(2026, 4, 27, 12, 0, 0),
    )
    assert rec_dir.exists()
    assert delete_capture(tmp_path, "abcdef012345") is True
    assert not rec_dir.exists()


def test_delete_capture_returns_false_when_missing(tmp_path):
    assert delete_capture(tmp_path, "abcdef012345") is False


# ---------------------------------------------------------------------------
# request_retry
# ---------------------------------------------------------------------------


def test_request_retry_resets_failed_permanent(tmp_path):
    base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=base,
        metadata={"upload": dict(empty_upload_state(), status=STATUS_FAILED_PERMANENT)},
    )
    now = datetime(2026, 4, 27, 13, 0, 0, tzinfo=timezone.utc)
    assert request_retry(tmp_path, "abcdef012345", now=now) is True

    data = json.loads((tmp_path / "abcdef012345" / "record.json").read_text())
    assert data["metadata"]["upload"]["status"] == STATUS_FAILED_TRANSIENT
    assert data["metadata"]["upload"]["next_attempt_at"] == now.isoformat()


def test_request_retry_resets_auth_blocked(tmp_path):
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=datetime(2026, 4, 27, 12, 0, 0),
        metadata={"upload": dict(empty_upload_state(), status=STATUS_AUTH_BLOCKED)},
    )
    assert request_retry(tmp_path, "abcdef012345") is True
    data = json.loads((tmp_path / "abcdef012345" / "record.json").read_text())
    assert data["metadata"]["upload"]["status"] == STATUS_FAILED_TRANSIENT


def test_request_retry_refuses_uploaded(tmp_path):
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=datetime(2026, 4, 27, 12, 0, 0),
        metadata={"upload": dict(empty_upload_state(), status=STATUS_UPLOADED)},
    )
    assert request_retry(tmp_path, "abcdef012345") is False


def test_request_retry_refuses_dropped(tmp_path):
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=datetime(2026, 4, 27, 12, 0, 0),
        metadata={"dropped": {"reason": "gate_failed"}},
        write_audio=False,
    )
    assert request_retry(tmp_path, "abcdef012345") is False


def test_request_retry_returns_false_when_missing(tmp_path):
    assert request_retry(tmp_path, "abcdef012345") is False


def test_request_retry_validates_id(tmp_path):
    with pytest.raises(ValueError):
        request_retry(tmp_path, "../escape")


# ---------------------------------------------------------------------------
# is_valid_id + summary_to_dict
# ---------------------------------------------------------------------------


def test_is_valid_id():
    assert is_valid_id("abcdef012345")
    assert not is_valid_id("ABCDEF012345")  # uppercase rejected
    assert not is_valid_id("")
    assert not is_valid_id("abc")
    assert not is_valid_id("../escape")


def test_summary_to_dict_serialises_status_string(tmp_path):
    base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    _write_record(
        tmp_path,
        "abcdef012345",
        started_at=base,
        metadata={"upload": dict(empty_upload_state(), status=STATUS_UPLOADED)},
    )
    [summary] = enumerate_captures(tmp_path, {})
    payload = summary_to_dict(summary)
    assert payload["status"] == "uploaded"
    assert payload["bucket"] == "uploaded"
    assert payload["has_audio"] is True


# ---------------------------------------------------------------------------
# Sink: write_dropped + pruning
# ---------------------------------------------------------------------------


def test_sink_write_dropped_creates_stub_no_audio(tmp_path):
    from sayzo_agent.sink import CaptureSink

    sink = CaptureSink(tmp_path)
    rec_id = sink.write_dropped(
        datetime(2026, 4, 27, 12, 0, 0),
        datetime(2026, 4, 27, 12, 0, 22),
        "gate_failed",
        extra={"gate_reason": "no_user"},
    )
    rec_dir = tmp_path / rec_id
    assert (rec_dir / "record.json").exists()
    assert not (rec_dir / "audio.opus").exists()
    data = json.loads((rec_dir / "record.json").read_text())
    assert data["metadata"]["dropped"]["reason"] == "gate_failed"
    assert data["metadata"]["dropped"]["gate_reason"] == "no_user"
    assert data["metadata"]["dropped"]["reason_label"]


def test_sink_write_dropped_prunes_oldest_beyond_cap(tmp_path, monkeypatch):
    """The cap is normally 100 — patch it to a small number to keep the test
    fast and still exercise the prune path."""
    from sayzo_agent import sink as sink_module

    monkeypatch.setattr(sink_module, "DROPPED_STUB_CAP", 3)
    sink = sink_module.CaptureSink(tmp_path)

    base = datetime(2026, 4, 27, 12, 0, 0)
    ids = []
    for i in range(5):
        rec_id = sink.write_dropped(
            base + timedelta(minutes=i),
            base + timedelta(minutes=i, seconds=10),
            "gate_failed",
        )
        ids.append(rec_id)

    surviving = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert len(surviving) == 3, surviving
    # The two oldest should be gone.
    assert ids[0] not in surviving
    assert ids[1] not in surviving
    # The three most-recent survive.
    assert set(ids[2:]) == set(surviving)
