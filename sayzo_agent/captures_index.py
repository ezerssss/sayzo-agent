"""Read-only enumeration + status derivation for the Settings Captures pane.

Pure logic on top of `upload_retry.read_record_from_dir` / `write_record_atomic`.
The Settings subprocess calls into here from the JSON-RPC bridge; the live
agent doesn't import this module.

Status mapping is the single place we translate the upload retry state-machine
(`retry.STATUS_*`) into UI-friendly strings + tones. `friendly_label` is the
contract the frontend reads — keep the wording calm and non-technical.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from .retry import (
    STATUS_AUTH_BLOCKED,
    STATUS_CREDIT_BLOCKED,
    STATUS_FAILED_PERMANENT,
    STATUS_FAILED_TRANSIENT,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    STATUS_UPLOADED,
)
from .sink import deserialize_record
from .upload_retry import read_record_from_dir, write_record_atomic

log = logging.getLogger(__name__)


# Capture id shape: 12 lowercase hex chars. Matches `uuid.uuid4().hex[:12]` in
# sink.write / sink.write_dropped. Used everywhere we accept an id from the
# bridge — anything else is a path-traversal attempt.
ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")

Bucket = Literal["in_progress", "uploaded", "failed", "skipped"]
Tone = Literal["gray", "blue", "green", "amber", "red"]


class CaptureStatus(str, Enum):
    PROCESSING = "processing"           # in-flight in the agent, no record on disk yet
    PENDING = "pending"                 # on disk, never attempted
    UPLOADING = "uploading"             # status=in_flight on disk
    UPLOADED = "uploaded"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    CREDIT_BLOCKED = "credit_blocked"
    AUTH_BLOCKED = "auth_blocked"
    DROPPED = "dropped"                 # metadata.dropped present, no audio.opus


@dataclass
class CaptureSummary:
    id: str
    title: str
    started_at: str           # ISO
    ended_at: str             # ISO
    duration_secs: float
    status: CaptureStatus
    bucket: Bucket
    badge_label: str
    badge_tone: Tone
    detail: Optional[str]
    attempts: int
    next_attempt_at: Optional[str]
    has_audio: bool
    is_processing: bool
    dropped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


_UPLOAD_STATUS_TO_CAPTURE: dict[str, CaptureStatus] = {
    STATUS_PENDING: CaptureStatus.PENDING,
    STATUS_IN_FLIGHT: CaptureStatus.UPLOADING,
    STATUS_UPLOADED: CaptureStatus.UPLOADED,
    STATUS_FAILED_TRANSIENT: CaptureStatus.FAILED_TRANSIENT,
    STATUS_FAILED_PERMANENT: CaptureStatus.FAILED_PERMANENT,
    STATUS_CREDIT_BLOCKED: CaptureStatus.CREDIT_BLOCKED,
    STATUS_AUTH_BLOCKED: CaptureStatus.AUTH_BLOCKED,
}


def derive_status(metadata: dict | None) -> CaptureStatus:
    """Map a record's metadata to its UI status. Dropped > upload state."""
    if not metadata:
        return CaptureStatus.PENDING
    if metadata.get("dropped"):
        return CaptureStatus.DROPPED
    upload = metadata.get("upload") or {}
    raw = upload.get("status") or STATUS_PENDING
    return _UPLOAD_STATUS_TO_CAPTURE.get(raw, CaptureStatus.PENDING)


def bucket_for(status: CaptureStatus) -> Bucket:
    if status == CaptureStatus.UPLOADED:
        return "uploaded"
    if status in (CaptureStatus.FAILED_TRANSIENT, CaptureStatus.FAILED_PERMANENT):
        return "failed"
    if status == CaptureStatus.DROPPED:
        return "skipped"
    return "in_progress"


def friendly_label(
    status: CaptureStatus,
    error_message: str | None = None,
    dropped_reason: str | None = None,
) -> tuple[str, Tone]:
    """User-facing label + badge tone. Plain English, no jargon."""
    if status == CaptureStatus.PROCESSING:
        return "Sayzo is analyzing this", "blue"
    if status == CaptureStatus.PENDING:
        return "Waiting to upload", "gray"
    if status == CaptureStatus.UPLOADING:
        return "Uploading…", "blue"
    if status == CaptureStatus.UPLOADED:
        return "Saved to your account", "green"
    if status == CaptureStatus.FAILED_TRANSIENT:
        return "Will try again soon", "amber"
    if status == CaptureStatus.FAILED_PERMANENT:
        return "Couldn't upload", "red"
    if status == CaptureStatus.CREDIT_BLOCKED:
        return "Paused — Sayzo limit reached", "amber"
    if status == CaptureStatus.AUTH_BLOCKED:
        return "Sign in to keep uploading", "amber"
    if status == CaptureStatus.DROPPED:
        return _DROPPED_LABELS.get(dropped_reason or "", "Skipped"), "gray"
    return "Unknown", "gray"


_DROPPED_LABELS: dict[str, str] = {
    "gate_failed": "Skipped — not enough conversation",
    "non_english": "Skipped — wasn't English",
    "empty_transcript": "Skipped — nothing was transcribed",
    "llm_rejected": "Sayzo decided not to keep this",
}


def _detail_text(
    status: CaptureStatus,
    upload: dict | None,
    dropped: dict | None,
) -> Optional[str]:
    """One-line muted detail under the badge. None to omit."""
    if status == CaptureStatus.DROPPED and dropped:
        # Friendly explanation, not the raw reason key.
        reason = dropped.get("reason") or ""
        if reason == "gate_failed":
            return "This was very short or mostly silence."
        if reason == "non_english":
            lang = (dropped.get("detected_lang") or "").upper()
            return f"Sayzo only coaches English right now (heard {lang})." if lang else "Sayzo only coaches English right now."
        if reason == "empty_transcript":
            return "Sayzo couldn't make out any speech."
        if reason == "llm_rejected":
            return "It didn't look like a real conversation."
        return None
    if status in (
        CaptureStatus.FAILED_TRANSIENT,
        CaptureStatus.FAILED_PERMANENT,
        CaptureStatus.CREDIT_BLOCKED,
        CaptureStatus.AUTH_BLOCKED,
    ):
        msg = (upload or {}).get("last_error_message")
        if msg:
            # Strip any raw HTTP prefix; keep it short.
            cleaned = str(msg).strip()
            if len(cleaned) > 140:
                cleaned = cleaned[:137] + "…"
            return cleaned
    return None


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _iter_capture_dirs(captures_dir: Path) -> Iterable[Path]:
    """Mirror of `upload_retry._iter_capture_dirs` to keep this module
    importable without pulling in the full retry stack."""
    import os
    try:
        with os.scandir(captures_dir) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                if entry.name.startswith("."):
                    continue
                yield Path(entry.path)
    except FileNotFoundError:
        return


def _duration_secs(started_at: str, ended_at: str) -> float:
    try:
        s = datetime.fromisoformat(started_at)
        e = datetime.fromisoformat(ended_at)
        return max(0.0, round((e - s).total_seconds(), 1))
    except Exception:
        return 0.0


def _summary_from_processing(proc_id: str, info: dict) -> CaptureSummary:
    started = info.get("started_at") or datetime.now(timezone.utc).isoformat()
    duration = float(info.get("duration_secs") or 0.0)
    label = info.get("label") or "Sayzo is analyzing this"
    return CaptureSummary(
        id=proc_id,
        title="Untitled meeting",
        started_at=started,
        ended_at=started,  # synthetic — no real end yet
        duration_secs=duration,
        status=CaptureStatus.PROCESSING,
        bucket="in_progress",
        badge_label=label,
        badge_tone="blue",
        detail=None,
        attempts=0,
        next_attempt_at=None,
        has_audio=False,
        is_processing=True,
    )


def _summary_from_record(rec_dir: Path) -> Optional[CaptureSummary]:
    """Read one capture dir into a summary. Returns None on corrupt/missing."""
    try:
        record = read_record_from_dir(rec_dir)
    except FileNotFoundError:
        return None
    except Exception:
        log.debug("[captures_index] skipping corrupt %s", rec_dir, exc_info=True)
        return None

    metadata = record.metadata or {}
    status = derive_status(metadata)
    upload = metadata.get("upload") or {}
    dropped = metadata.get("dropped") or {}
    dropped_reason = dropped.get("reason") if dropped else None

    label, tone = friendly_label(status, upload.get("last_error_message"), dropped_reason)
    detail = _detail_text(status, upload, dropped)

    audio_path = rec_dir / "audio.opus"
    has_audio = audio_path.exists() and not dropped

    title = (record.title or "").strip() or "Untitled meeting"

    return CaptureSummary(
        id=record.id,
        title=title,
        started_at=record.started_at.isoformat(),
        ended_at=record.ended_at.isoformat(),
        duration_secs=round((record.ended_at - record.started_at).total_seconds(), 1),
        status=status,
        bucket=bucket_for(status),
        badge_label=label,
        badge_tone=tone,
        detail=detail,
        attempts=int(upload.get("attempts") or 0),
        next_attempt_at=upload.get("next_attempt_at"),
        has_audio=has_audio,
        is_processing=False,
        dropped_reason=dropped_reason,
    )


def enumerate_captures(
    captures_dir: Path,
    processing_state: dict | None = None,
) -> list[CaptureSummary]:
    """Return all captures (synthetic processing + on-disk), processing first
    then by `started_at` desc.

    `processing_state` is the dict returned by the agent's IPC method; safe to
    pass `None` or `{}` if the agent isn't running.
    """
    out: list[CaptureSummary] = []
    on_disk_ids: set[str] = set()

    # Read disk first so we can dedup against any processing entry whose
    # proc_id has already landed on disk (the agent reuses the proc_id as
    # the eventual record id, so during the brief window between sink.write
    # completing and the proc_id being popped, both views could see it).
    for rec_dir in _iter_capture_dirs(captures_dir):
        if not (rec_dir / "record.json").exists():
            continue
        s = _summary_from_record(rec_dir)
        if s is not None:
            on_disk_ids.add(s.id)
            out.append(s)

    if processing_state:
        for proc_id, info in processing_state.items():
            proc_id_str = str(proc_id)
            if proc_id_str in on_disk_ids:
                continue
            try:
                out.append(_summary_from_processing(proc_id_str, info or {}))
            except Exception:
                log.debug("[captures_index] bad processing entry %r", proc_id, exc_info=True)

    # Processing rows go to the top regardless of timestamp; everything else
    # sorts most-recent-first so the user sees the meeting they just had.
    def sort_key(s: CaptureSummary) -> tuple[int, str]:
        return (0 if s.is_processing else 1, _negate_iso(s.started_at))

    out.sort(key=sort_key)
    return out


def _negate_iso(iso: str) -> str:
    """Sort an ISO timestamp descending by inverting char codes per position.
    Avoids parsing — just need a stable reverse-lex key. Fall back to the raw
    string if anything weird happens."""
    try:
        return "".join(chr(0xFFFF - ord(c)) for c in iso)
    except Exception:
        return iso


# ---------------------------------------------------------------------------
# Mutations (delete, retry-now)
# ---------------------------------------------------------------------------


def is_valid_id(capture_id: str) -> bool:
    return bool(capture_id) and bool(ID_PATTERN.match(capture_id))


def delete_capture(captures_dir: Path, capture_id: str) -> bool:
    """Remove the capture directory entirely. Validates id shape. Returns
    True if the directory existed and was removed."""
    if not is_valid_id(capture_id):
        raise ValueError(f"invalid capture id: {capture_id!r}")
    rec_dir = captures_dir / capture_id
    if not rec_dir.exists():
        return False
    shutil.rmtree(rec_dir)
    return True


def request_retry(
    captures_dir: Path,
    capture_id: str,
    now: datetime | None = None,
) -> bool:
    """Mark a record as immediately due for retry.

    Flips terminal-failed and blocked states back to `failed_transient` with
    `next_attempt_at = now` so the next upload sweep picks it up. Refuses
    records that are already uploaded.
    """
    if not is_valid_id(capture_id):
        raise ValueError(f"invalid capture id: {capture_id!r}")
    rec_dir = captures_dir / capture_id
    if not (rec_dir / "record.json").exists():
        return False
    record = read_record_from_dir(rec_dir)
    metadata = dict(record.metadata or {})
    if metadata.get("dropped"):
        return False  # Dropped stubs have no upload story.
    upload = dict(metadata.get("upload") or {})
    if upload.get("status") == STATUS_UPLOADED:
        return False
    now = now or datetime.now(timezone.utc)
    upload["status"] = STATUS_FAILED_TRANSIENT
    upload["next_attempt_at"] = now.isoformat()
    metadata["upload"] = upload
    record.metadata = metadata
    write_record_atomic(rec_dir, record)
    return True


# ---------------------------------------------------------------------------
# Serialization helper for the bridge
# ---------------------------------------------------------------------------


def summary_to_dict(s: CaptureSummary) -> dict[str, Any]:
    """asdict + ensure the CaptureStatus enum serialises as its string value."""
    d = asdict(s)
    d["status"] = s.status.value
    return d
