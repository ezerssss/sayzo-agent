"""Silero VAD wrapper.

Stateful per-source: feed it 16 kHz float32 frames + each frame's
``frame_mono_ts`` (the ``time.monotonic()`` value of the frame's first
sample), and it emits ``SpeechSegment`` events when contiguous voiced
regions end (with hangover). Segment ``start_ts`` / ``end_ts`` are
monotonic seconds — the detector subtracts ``session_t0_mono`` to get
session-relative seconds at write time.

Backend note: we load Silero via the torch JIT path
(``load_silero_vad(onnx=False)``) — the JIT model is silero-vad's
default and only requires ``torch`` + ``torchaudio``, both of which
``feed()`` already imports for tensor work. Using the ONNX backend
would force a third runtime (``onnxruntime``, ~39 MB) on top of torch
without changing the inference pathway, and was the source of the
v3.0.0 regression where dropping ``faster-whisper`` silently took
``onnxruntime`` with it (faster-whisper was its only transitive
provider — silero-vad declares onnxruntime as an ``[onnx-cpu]`` extra,
not a real dep). Don't reintroduce ``onnx=True`` without first making
``onnxruntime`` a direct dep in pyproject.toml.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Optional

import numpy as np

from .models import SpeechSegment, Source

log = logging.getLogger(__name__)

# Shared across SileroVAD instances — the torch JIT load is ~150 ms on
# first call and isn't worth racing two of them in parallel. Holding
# this around both ``_ensure_loaded`` and any pre-warm call from a
# background thread guarantees we pay the cost exactly once.
_LOAD_LOCK = threading.Lock()


class SileroVAD:
    """Lightweight stateful VAD around the Silero model (torch JIT backend).

    Silero expects 16 kHz mono float32 in chunks of 512 samples (32 ms).
    We accept arbitrary frame sizes and re-chunk internally.
    """

    SILERO_CHUNK = 512  # samples at 16 kHz
    SAMPLE_RATE = 16000
    _CHUNK_DURATION_SECS = SILERO_CHUNK / SAMPLE_RATE

    def __init__(
        self,
        source: Source,
        threshold: float = 0.5,
        min_speech_ms: int = 200,
        hangover_ms: int = 300,
    ) -> None:
        self.source = source
        self.threshold = threshold
        self.min_speech_secs = min_speech_ms / 1000.0
        self.hangover_secs = hangover_ms / 1000.0

        # silero-vad's torch JIT load is ~150 ms on first call. Cheap
        # enough on its own, but the agent constructs two SileroVAD
        # instances at boot and ``Agent._prewarm_vads`` loads both off
        # the event loop, so this stays lazy — the executor pre-warm
        # pays the cost before the user arms.
        self._model = None

        self._buf = np.zeros(0, dtype=np.float32)
        # Monotonic time of ``_buf[0]``. Set when ``_buf`` is empty and a
        # new frame arrives; advances by one chunk's duration each time
        # we consume a chunk from the front of ``_buf``.
        self._buf_start_mono: Optional[float] = None
        self._in_speech = False
        self._speech_start_mono: Optional[float] = None
        self._last_voiced_mono: Optional[float] = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        # Lock so a background pre-warm thread (Agent._prewarm_vads) and a
        # concurrent first ``feed()`` from the consume loop can't race and
        # pay the silero-vad load cost twice.
        with _LOAD_LOCK:
            if self._model is not None:
                return
            from silero_vad import load_silero_vad
            self._model = load_silero_vad(onnx=False)

    def reset(self) -> None:
        """Reset the VAD to a cold-start state.

        Called when the agent re-arms after being disarmed. The internal
        buffer + in-progress segment state rewind so the next frame is
        treated as the first of a freshly-started stream.
        """
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_mono = None
        self._in_speech = False
        self._speech_start_mono = None
        self._last_voiced_mono = None
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

    def feed(self, frame: np.ndarray, frame_mono_ts: float) -> Iterator[SpeechSegment]:
        """Feed one PCM frame; yield any SpeechSegment(s) that closed.

        ``frame_mono_ts`` is the ``time.monotonic()`` value of the
        FIRST sample in ``frame``. Yielded segments' ``start_ts`` /
        ``end_ts`` are monotonic seconds, anchored to the actual capture
        time of each chunk Silero processed.
        """
        import torch

        self._ensure_loaded()
        # Anchor ``_buf_start_mono`` when the buffer is empty. If there
        # are leftover samples from a previous ``feed()`` call,
        # ``_buf_start_mono`` already tracks ``_buf[0]`` — we just append
        # at the tail and the anchor stays correct.
        if len(self._buf) == 0:
            self._buf_start_mono = frame_mono_ts
        self._buf = np.concatenate([self._buf, frame.astype(np.float32, copy=False)])

        while len(self._buf) >= self.SILERO_CHUNK:
            chunk = self._buf[: self.SILERO_CHUNK]
            self._buf = self._buf[self.SILERO_CHUNK :]
            assert self._buf_start_mono is not None  # set above when buf was empty
            chunk_start_mono = self._buf_start_mono
            chunk_end_mono = chunk_start_mono + self._CHUNK_DURATION_SECS
            self._buf_start_mono = chunk_end_mono

            with torch.no_grad():
                prob = float(
                    self._model(torch.from_numpy(chunk), self.SAMPLE_RATE).item()
                )
            voiced = prob >= self.threshold

            if voiced:
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_start_mono = chunk_start_mono
                self._last_voiced_mono = chunk_end_mono
                continue

            if self._in_speech and self._last_voiced_mono is not None:
                silence = chunk_end_mono - self._last_voiced_mono
                if silence >= self.hangover_secs:
                    assert self._speech_start_mono is not None
                    duration = self._last_voiced_mono - self._speech_start_mono
                    if duration >= self.min_speech_secs:
                        yield SpeechSegment(
                            source=self.source,
                            start_ts=self._speech_start_mono,
                            end_ts=self._last_voiced_mono,
                        )
                    self._in_speech = False
                    self._speech_start_mono = None
                    self._last_voiced_mono = None

    def flush(self) -> Iterator[SpeechSegment]:
        """Force-close any in-progress segment (e.g. on session close)."""
        if (
            self._in_speech
            and self._speech_start_mono is not None
            and self._last_voiced_mono is not None
        ):
            duration = self._last_voiced_mono - self._speech_start_mono
            if duration >= self.min_speech_secs:
                yield SpeechSegment(
                    source=self.source,
                    start_ts=self._speech_start_mono,
                    end_ts=self._last_voiced_mono,
                )
        self._in_speech = False
        self._speech_start_mono = None
        self._last_voiced_mono = None
