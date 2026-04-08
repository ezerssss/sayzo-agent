"""faster-whisper transcription wrapper."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .config import STTConfig

log = logging.getLogger(__name__)


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class TranscribedSegment:
    start: float
    end: float
    text: str
    words: list[Word]


class WhisperSTT:
    def __init__(self, cfg: STTConfig, models_dir: str | None = None) -> None:
        self.cfg = cfg
        self.models_dir = models_dir
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        log.info("loading faster-whisper model=%s compute=%s device=%s", self.cfg.model, self.cfg.compute_type, self.cfg.device)
        self._model = WhisperModel(
            self.cfg.model,
            device=self.cfg.device,
            compute_type=self.cfg.compute_type,
            download_root=self.models_dir,
        )

    def transcribe_pcm16(self, pcm16: bytes, sample_rate: int = 16000) -> list[TranscribedSegment]:
        if not pcm16:
            return []
        self._ensure_loaded()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000:
            raise ValueError("Whisper requires 16 kHz input")
        assert self._model is not None
        segments_iter, info = self._model.transcribe(
            audio,
            language=self.cfg.language,
            word_timestamps=True,
            # Re-run VAD inside Whisper to drop silent gaps. Without this,
            # Whisper hallucinates "Thank you", "Thanks for watching", etc.
            # on quiet stretches.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            # Don't let prior context bias later segments — another hallucination
            # source.
            condition_on_previous_text=False,
            # Tighten thresholds against low-confidence ghost segments.
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        out: list[TranscribedSegment] = []
        for s in segments_iter:
            words = [Word(start=w.start, end=w.end, text=w.word) for w in (s.words or [])]
            out.append(TranscribedSegment(start=s.start, end=s.end, text=s.text.strip(), words=words))
        log.info("transcribed %d segments (lang=%s)", len(out), info.language)
        return out
