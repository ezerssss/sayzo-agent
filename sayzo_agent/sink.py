"""Persist ConversationRecord + compressed audio to disk.

Schema (v3.0+): the agent owns id / timestamps / a synthetic local placeholder
title / arbitrary metadata only. Transcript, speaker labels, and the real
title/summary live server-side; ``CapturePoller`` later overwrites the local
placeholder title with the real one for display in Settings → Captures.

Two serializers:

- ``serialize_record`` — full local schema (id, timestamps, title, summary,
  metadata). Goes to disk in ``record.json``.
- ``serialize_record_for_upload`` — upload-only subset (id, timestamps,
  metadata.close_reason). The HTTP body — server doesn't need our placeholder
  or local-only metadata blobs.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from .models import ConversationRecord

log = logging.getLogger(__name__)


# Filename of the encoded stereo Opus blob inside each capture directory.
# Hard-coded — every consumer (sink, upload client, captures pane) derives
# the path from the rec_id + this constant.
AUDIO_FILENAME = "audio.opus"


# Reasons a session can be dropped (silent skips that the Captures pane
# surfaces under "Skipped"). The labels are the user-facing copy. Legacy
# reason values from older agent versions are rendered via
# ``captures_index._DROPPED_LABELS`` on read.
DROPPED_REASON_LABELS: dict[str, str] = {
    "gate_failed": "Skipped — too short",
}

# Most-recent N dropped stubs to keep on disk. Older are pruned at write time
# so the captures dir doesn't grow unbounded for users who frequently fail
# the gate (e.g. brief hot-mic moments).
DROPPED_STUB_CAP = 100


def encode_opus_stereo(
    mic_pcm16: bytes,
    sys_pcm16: bytes,
    out_path: Path,
    sample_rate: int = 16000,
    bitrate: int = 96000,
    application: str = "audio",
) -> None:
    """Encode mic (L) + system (R) as a single stereo Opus file via PyAV.

    Left=mic / right=system is load-bearing: the server's per-speaker
    diarization (Deepgram multichannel) keys off the physical channel split.
    Never collapse to mono.

    ``application`` maps to libopus's own ``application`` flag:
      - ``audio`` (default) — libopus's general-purpose mode; preserves high
        frequencies, stereo imaging, and transients. Good for captures that
        mix speech with music/game/video audio on the system channel.
      - ``voip`` — speech-optimized DSP. Lower bitrates survive better but a
        speech-band filter is applied that audibly degrades non-speech.
      - ``lowdelay`` — sacrifices quality for reduced algorithmic delay.
    """
    import av  # lazy

    mic = np.frombuffer(mic_pcm16, dtype=np.int16)
    sys = np.frombuffer(sys_pcm16, dtype=np.int16)
    n = max(len(mic), len(sys))
    if len(mic) < n:
        mic = np.concatenate([mic, np.zeros(n - len(mic), dtype=np.int16)])
    if len(sys) < n:
        sys = np.concatenate([sys, np.zeros(n - len(sys), dtype=np.int16)])
    interleaved = np.empty(2 * n, dtype=np.int16)
    interleaved[0::2] = mic
    interleaved[1::2] = sys

    out_path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(out_path), mode="w", format="ogg")
    stream = container.add_stream("libopus", rate=sample_rate)
    # Set encoder-private options before the first encode() call. libopus reads
    # these when the encoder is opened implicitly on first encode.
    stream.options = {
        "application": application,
        "vbr": "on",
        "compression_level": "10",
        "frame_duration": "20",
    }
    stream.bit_rate = bitrate
    stream.layout = "stereo"

    frame = av.AudioFrame.from_ndarray(
        interleaved.reshape(1, -1),
        format="s16",
        layout="stereo",
    )
    frame.sample_rate = sample_rate
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()


def _placeholder_title(
    arm_app_key: Optional[str],
    arm_app_display: Optional[str],
    started_at: datetime,
) -> str:
    """Build the local placeholder title shown in Settings → Captures.

    Deterministic so the pane has something readable as soon as the capture
    lands on disk. ``CapturePoller`` later overwrites it with the real
    server-generated title once ``GET /api/captures/{id}`` reports the
    capture is past the transcribed/analyzed milestones.

    Prefers ``arm_app_display`` (the user-facing name from DetectorSpec, e.g.
    "Microsoft Teams" / "Google Meet") over ``arm_app_key.title()`` (which
    produces gross strings like "Teams_Desktop" / "Gmeet" / "8X8"). Falls
    back to the lowercase key for legacy / custom detector specs that didn't
    set a display name.
    """
    stamp = started_at.strftime("%Y-%m-%d %H:%M")
    name = (arm_app_display or "").strip() or (
        arm_app_key.title() if arm_app_key else ""
    )
    if name:
        return f"{name} call · {stamp}"
    return f"Conversation · {stamp}"


# Wall-clock label like "2:30 pm" — converts the stored UTC timestamp to
# the user's local TZ at CAPTURE time (not display time). Lives in this
# module so ``CaptureSink.write`` can persist the result in
# ``record.metadata["local_clock_label"]`` and downstream consumers
# (``capture_poller._source_label``) just read the cached string. Locking
# the TZ at write time avoids the bug where a user who travels between
# session close and insight fire would otherwise see the wrong wall-clock
# time on the post-capture card (``astimezone()`` reads the OS TZ at call
# time, not at session close). ``%I`` produces "01"–"12"; ``.lstrip("0")``
# trims the hour's leading zero. ``%I`` is guaranteed never to emit "00",
# so the lstrip can't swallow the whole hour.
def local_clock_label(ts: datetime) -> str:
    try:
        local = ts.astimezone() if ts.tzinfo is not None else ts
        return local.strftime("%I:%M %p").lstrip("0").lower()
    except Exception:
        return ""


def serialize_record(record: ConversationRecord) -> dict:
    """Full local schema — what goes to disk in ``record.json``."""
    return {
        "id": record.id,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "title": record.title,
        "summary": record.summary,
        "metadata": record.metadata,
    }


def serialize_record_for_upload(record: ConversationRecord) -> dict:
    """Upload-only subset — what goes into the multipart ``record`` field.

    The server only needs id + timestamps + close_reason to dedupe, file,
    and emit lifecycle hooks. Title/summary it generates itself; transcript
    + diarization come from Deepgram; ``metadata.upload`` and
    ``metadata.echo_guard`` are local-only diagnostics.
    """
    return {
        "id": record.id,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "metadata": {
            "close_reason": record.metadata.get("close_reason"),
        },
    }


def deserialize_record(data: dict) -> ConversationRecord:
    """Read a record.json dict into ConversationRecord.

    Pre-3.0 records carry extra top-level keys (``transcript``,
    ``audio_path``, ``relevant_span``) and extra metadata keys
    (``local_llm_used``, ``placeholder_title``). Those simply aren't read
    here — `ConversationRecord` doesn't declare them — so legacy files
    deserialize fine and the extra keys stay in `data` for whoever wants
    them.
    """
    return ConversationRecord(
        id=data["id"],
        started_at=datetime.fromisoformat(data["started_at"]),
        ended_at=datetime.fromisoformat(data["ended_at"]),
        title=data.get("title", ""),
        summary=data.get("summary", ""),
        metadata=data.get("metadata", {}),
    )


def read_record_from_dir(rec_dir: Path) -> ConversationRecord:
    """Read record.json from a capture directory into a ConversationRecord."""
    with (rec_dir / "record.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return deserialize_record(data)


def write_record_atomic(rec_dir: Path, record: ConversationRecord) -> None:
    """Write record.json via temp-file + os.replace (atomic on Windows + POSIX)."""
    target = rec_dir / "record.json"
    tmp = rec_dir / f"record.json.tmp-{os.getpid()}-{time.monotonic_ns()}"
    payload = json.dumps(serialize_record(record), indent=2, ensure_ascii=False)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


class CaptureSink:
    def __init__(
        self,
        captures_dir: Path,
        opus_bitrate: int = 96000,
        opus_application: str = "audio",
    ) -> None:
        self.captures_dir = captures_dir
        self.opus_bitrate = opus_bitrate
        self.opus_application = opus_application

    def write(
        self,
        arm_app_key: Optional[str],
        started_at: datetime,
        ended_at: datetime,
        mic_pcm16: bytes,
        sys_pcm16: bytes,
        sample_rate: int = 16000,
        metadata: dict | None = None,
        rec_id: str | None = None,
        *,
        arm_app_display: Optional[str] = None,
    ) -> ConversationRecord:
        """Persist a kept session: encode the stereo Opus blob + write
        ``record.json`` with the local placeholder title.

        The capture poller will later overwrite ``title`` / ``summary`` once
        the server has the real ones, but ``metadata.arm_app_key`` /
        ``arm_app_display`` / ``local_clock_label`` remain — the insight
        card's source-anchor chip derives from those, not from ``title``,
        so the chip's wording stays deterministic regardless of whether
        the server's title pass succeeded.
        """
        rec_id = rec_id or uuid.uuid4().hex[:12]
        rec_dir = self.captures_dir / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)

        encode_opus_stereo(
            bytes(mic_pcm16),
            bytes(sys_pcm16),
            rec_dir / AUDIO_FILENAME,
            sample_rate=sample_rate,
            bitrate=self.opus_bitrate,
            application=self.opus_application,
        )

        title = _placeholder_title(arm_app_key, arm_app_display, started_at)
        # Persist the local wall-clock label + arm-app identity at write
        # time. Locking these to the capture moment (not the fire moment)
        # makes the post-capture insight chip's "[time] X call" anchor
        # robust against: (1) TZ drift if the user travels between
        # capture and fire, (2) server-side title-pass flakiness — the
        # chip no longer reads ``record.title`` so it can't be empty or
        # weird when the server fails to summarize.
        meta = dict(metadata) if metadata else {}
        meta.setdefault("local_clock_label", local_clock_label(started_at))
        if arm_app_key:
            meta.setdefault("arm_app_key", arm_app_key)
        if arm_app_display:
            meta.setdefault("arm_app_display", arm_app_display)
        record = ConversationRecord(
            id=rec_id,
            started_at=started_at,
            ended_at=ended_at,
            title=title,
            summary="",
            metadata=meta,
        )
        json_path = rec_dir / "record.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(serialize_record(record), f, indent=2, ensure_ascii=False)
        log.info("[sink] wrote capture id=%s title=%r", rec_id, title)
        log.info("[sink]   record:     %s", json_path.resolve())
        log.info("[sink]   audio:      %s", (rec_dir / AUDIO_FILENAME).resolve())
        return record

    def write_dropped(
        self,
        started_at: datetime,
        ended_at: datetime,
        reason: str,
        *,
        extra: dict | None = None,
        rec_id: str | None = None,
    ) -> str:
        """Persist a tiny stub for a dropped session — no audio.

        Used by `app.py` when the cheap gate fails so the user can see in
        Settings → Captures that Sayzo heard them and decided not to keep
        this one. Returns the stub's record id.
        """
        rec_id = rec_id or uuid.uuid4().hex[:12]
        rec_dir = self.captures_dir / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)

        dropped_meta: dict = {
            "reason": reason,
            "reason_label": DROPPED_REASON_LABELS.get(reason, "Skipped"),
        }
        if extra:
            dropped_meta.update(extra)

        # Mark the upload state terminal at write-time so the retry sweep
        # never picks these stubs up. Without this they look like legacy
        # records (missing metadata.upload), the sweep treats them as
        # pending, tries to upload (no audio file → fails), and logs a
        # warning per stub on every sweep. See retry.STATUS_DISCARDED_LOCALLY.
        from .retry import discarded_locally_state

        record = ConversationRecord(
            id=rec_id,
            started_at=started_at,
            ended_at=ended_at,
            title="",
            summary="",
            metadata={
                "dropped": dropped_meta,
                "upload": discarded_locally_state(),
            },
        )
        json_path = rec_dir / "record.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(serialize_record(record), f, indent=2, ensure_ascii=False)
        log.info("[sink] dropped stub id=%s reason=%s", rec_id, reason)

        try:
            self._prune_dropped_stubs()
        except Exception:
            log.debug("[sink] dropped-stub pruning failed", exc_info=True)
        return rec_id

    def _prune_dropped_stubs(self) -> None:
        """Trim oldest dropped stubs beyond DROPPED_STUB_CAP. Operates only on
        directories whose record.json has metadata.dropped — never touches
        kept captures."""
        if not self.captures_dir.exists():
            return
        stubs: list[tuple[datetime, Path]] = []
        for entry in self.captures_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            rec_json = entry / "record.json"
            if not rec_json.exists():
                continue
            try:
                with rec_json.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                log.debug(
                    "[sink] dropped-stub prune skipped %s (read/parse failed)",
                    rec_json, exc_info=True,
                )
                continue
            metadata = data.get("metadata") or {}
            if not metadata.get("dropped"):
                continue
            try:
                ts = datetime.fromisoformat(data.get("started_at"))
            except Exception:
                ts = datetime.fromtimestamp(0)
            stubs.append((ts, entry))
        if len(stubs) <= DROPPED_STUB_CAP:
            return
        stubs.sort(key=lambda p: p[0])  # oldest first
        excess = len(stubs) - DROPPED_STUB_CAP
        for _, entry in stubs[:excess]:
            try:
                shutil.rmtree(entry)
            except Exception:
                log.debug("[sink] failed to prune %s", entry, exc_info=True)
