"""Microphone capture via sounddevice."""
from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

from . import normalize_rms

log = logging.getLogger(__name__)


class MicCapture:
    """Captures mono PCM frames from the default (or named) input device.

    Frames are pushed onto an asyncio.Queue as float32 numpy arrays of shape
    (frame_samples,) at the configured sample rate.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        device: str | None = None,
        queue_maxsize: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.device = device
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_maxsize)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.debug("mic status: %s", status)
        # PortAudio can fire one more callback between stream.stop() and the
        # audio thread actually joining. If asyncio.run() has already closed
        # the loop by then, call_soon_threadsafe raises RuntimeError. Check
        # up front and catch as a backstop.
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # indata: (frames, channels) float32. We always use mono.
        mono = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        mono = normalize_rms(mono)
        try:
            loop.call_soon_threadsafe(self.queue.put_nowait, mono)
        except RuntimeError:
            pass

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            blocksize=self.frame_samples,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        log.info("mic capture started: device=%s sr=%d", self.device or "default", self.sample_rate)

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("mic capture stopped")
