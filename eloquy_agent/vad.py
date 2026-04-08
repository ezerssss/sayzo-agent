"""Silero VAD wrapper.

Stateful per-source: feed it 16 kHz float32 frames, it emits SpeechSegment
events when contiguous voiced regions end (with hangover).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Optional

import numpy as np

from .models import SpeechSegment, Source

log = logging.getLogger(__name__)


class SileroVAD:
    """Lightweight stateful VAD around the Silero ONNX model.

    Silero expects 16 kHz mono float32 in chunks of 512 samples (32 ms).
    We accept arbitrary frame sizes and re-chunk internally.
    """

    SILERO_CHUNK = 512  # samples at 16 kHz
    SAMPLE_RATE = 16000

    def __init__(
        self,
        source: Source,
        threshold: float = 0.5,
        min_speech_ms: int = 200,
        hangover_ms: int = 300,
    ) -> None:
        self.source = source
        self.threshold = threshold
        self.min_speech_samples = int(self.SAMPLE_RATE * min_speech_ms / 1000)
        self.hangover_samples = int(self.SAMPLE_RATE * hangover_ms / 1000)

        from silero_vad import load_silero_vad  # lazy import

        self._model = load_silero_vad(onnx=True)

        self._buf = np.zeros(0, dtype=np.float32)
        self._samples_seen = 0  # absolute samples consumed since session start
        self._in_speech = False
        self._speech_start: Optional[int] = None
        self._last_voiced: Optional[int] = None
        self._session_start_sample = 0  # for ts conversion

    def reset_session(self, start_sample: int = 0) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._samples_seen = start_sample
        self._in_speech = False
        self._speech_start = None
        self._last_voiced = None
        self._session_start_sample = start_sample
        try:
            self._model.reset_states()
        except Exception:
            pass

    def _to_ts(self, sample: int) -> float:
        return (sample - self._session_start_sample) / self.SAMPLE_RATE

    def feed(self, frame: np.ndarray) -> Iterator[SpeechSegment]:
        """Feed one PCM frame; yield any SpeechSegment(s) that closed."""
        import torch

        self._buf = np.concatenate([self._buf, frame.astype(np.float32, copy=False)])

        while len(self._buf) >= self.SILERO_CHUNK:
            chunk = self._buf[: self.SILERO_CHUNK]
            self._buf = self._buf[self.SILERO_CHUNK :]
            chunk_start = self._samples_seen
            self._samples_seen += self.SILERO_CHUNK

            with torch.no_grad():
                prob = float(
                    self._model(torch.from_numpy(chunk), self.SAMPLE_RATE).item()
                )
            voiced = prob >= self.threshold

            if voiced:
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_start = chunk_start
                self._last_voiced = self._samples_seen
            else:
                if self._in_speech and self._last_voiced is not None:
                    silence = self._samples_seen - self._last_voiced
                    if silence >= self.hangover_samples:
                        # close segment
                        assert self._speech_start is not None
                        duration_samples = self._last_voiced - self._speech_start
                        if duration_samples >= self.min_speech_samples:
                            seg = SpeechSegment(
                                source=self.source,
                                start_ts=self._to_ts(self._speech_start),
                                end_ts=self._to_ts(self._last_voiced),
                            )
                            yield seg
                        self._in_speech = False
                        self._speech_start = None
                        self._last_voiced = None

    def flush(self) -> Iterator[SpeechSegment]:
        """Force-close any in-progress segment (e.g. on session close)."""
        if self._in_speech and self._speech_start is not None and self._last_voiced is not None:
            duration_samples = self._last_voiced - self._speech_start
            if duration_samples >= self.min_speech_samples:
                yield SpeechSegment(
                    source=self.source,
                    start_ts=self._to_ts(self._speech_start),
                    end_ts=self._to_ts(self._last_voiced),
                )
        self._in_speech = False
        self._speech_start = None
        self._last_voiced = None
