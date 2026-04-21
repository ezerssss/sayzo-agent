"""Persist ConversationRecord + compressed audio to disk."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .models import ConversationRecord, TranscriptLine

log = logging.getLogger(__name__)


def encode_opus_stereo(
    mic_pcm16: bytes,
    sys_pcm16: bytes,
    out_path: Path,
    sample_rate: int = 16000,
    bitrate: int = 96000,
    application: str = "audio",
) -> None:
    """Encode mic (L) + system (R) as a single stereo Opus file via PyAV.

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


def serialize_record(record: ConversationRecord) -> dict:
    return {
        "id": record.id,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "title": record.title,
        "summary": record.summary,
        "transcript": [asdict(t) for t in record.transcript],
        "audio_path": record.audio_path,
        "relevant_span": list(record.relevant_span),
        "metadata": record.metadata,
    }


def deserialize_record(data: dict) -> ConversationRecord:
    return ConversationRecord(
        id=data["id"],
        started_at=datetime.fromisoformat(data["started_at"]),
        ended_at=datetime.fromisoformat(data["ended_at"]),
        transcript=[TranscriptLine(**t) for t in data["transcript"]],
        title=data.get("title", ""),
        summary=data["summary"],
        audio_path=data["audio_path"],
        relevant_span=tuple(data["relevant_span"]),
        metadata=data.get("metadata", {}),
    )


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
        transcript: list[TranscriptLine],
        title: str,
        summary: str,
        relevant_span: tuple[float, float],
        started_at: datetime,
        ended_at: datetime,
        mic_pcm16: bytes,
        sys_pcm16: bytes,
        sample_rate: int = 16000,
        metadata: dict | None = None,
    ) -> ConversationRecord:
        rec_id = uuid.uuid4().hex[:12]
        rec_dir = self.captures_dir / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)

        # Save the FULL session audio and transcript. We deliberately do NOT
        # crop to `relevant_span` here — small local LLMs routinely
        # under-estimate how much context a conversation needs, and once
        # cropped from the on-disk file that audio is gone forever. Keep
        # everything, store the LLM's span as metadata, let downstream
        # analysis decide whether to trust it.
        start_s, end_s = relevant_span
        audio_rel = "audio.opus"
        encode_opus_stereo(
            bytes(mic_pcm16),
            bytes(sys_pcm16),
            rec_dir / audio_rel,
            sample_rate=sample_rate,
            bitrate=self.opus_bitrate,
            application=self.opus_application,
        )

        record = ConversationRecord(
            id=rec_id,
            started_at=started_at,
            ended_at=ended_at,
            transcript=list(transcript),
            title=title,
            summary=summary,
            audio_path=audio_rel,
            relevant_span=(start_s, end_s),
            metadata=metadata or {},
        )
        json_path = rec_dir / "record.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(serialize_record(record), f, indent=2, ensure_ascii=False)
        log.info("[sink] wrote capture id=%s title=%r", rec_id, title)
        log.info("[sink]   transcript: %s", json_path.resolve())
        log.info("[sink]   audio:      %s", (rec_dir / audio_rel).resolve())
        return record
