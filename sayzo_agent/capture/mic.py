"""Microphone capture via sounddevice.

Each frame is enqueued as a ``(capture_mono_ts, pcm)`` tuple where
``capture_mono_ts`` is the ``time.monotonic()`` value corresponding to the
first sample in the frame.

Stamping strategy: ``time.monotonic()`` at callback entry, minus the
reported input latency and one frame duration. Two platform quirks we have
to handle:

1. **WASAPI's ``inputBufferAdcTime`` is broken**: PortAudio's WASAPI
   backend populates it with tiny values (e.g. 0.02) on a clock that
   doesn't match ``stream.time``. Correlating the two produces garbage
   timestamps. So we don't use ``inputBufferAdcTime`` at all — callback-
   time stamping is ~10-20 ms of constant bias, which the system side
   also has (batch end time), so cross-source alignment stays tight.

2. **WASAPI fires callbacks in pairs**: sounddevice sometimes delivers
   two 20 ms callbacks back-to-back with identical ``time.monotonic()``
   readings, then a ~40 ms gap to the next pair. Without handling this,
   adjacent frames get the same timestamp, the detector appears to see
   all frames arriving late in bursts, and cross-source alignment drifts
   by the pair delta (~40 ms). We enforce a strict per-frame spacing:
   each frame's timestamp is ``max(wall_clock_ts, last_ts +
   frame_duration)``. That de-aliases paired callbacks while still
   trusting wall-clock when it's ahead (real drops / queue backup).
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


class MicCapture:
    """Captures mono PCM frames from the default (or named) input device.

    Frames are pushed onto an asyncio.Queue as ``(capture_mono_ts, frame)``
    tuples. ``frame`` is a float32 numpy array of shape (frame_samples,) at
    the configured sample rate.
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
        self.frame_duration = self.frame_samples / sample_rate
        self.device = device
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = asyncio.Queue(maxsize=queue_maxsize)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Offset between "callback fires" and "first sample in indata was
        # captured". Combines the driver's reported input latency with one
        # frame duration (since the buffer was filled over the last
        # ``frame_duration`` seconds before the callback fired).
        self._capture_offset: float = 0.0
        # Last emitted capture_mono_ts. Used to de-alias WASAPI's paired
        # callbacks (two callbacks with the same wall-clock time.monotonic()).
        self._last_emitted_ts: float | None = None

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

        # Monotonic-at-callback minus the capture offset gives the mono
        # time of the FIRST sample in indata. This is what the detector's
        # gap-fill invariant expects.
        wall_clock_ts = time.monotonic() - self._capture_offset

        # De-alias WASAPI's paired callbacks: when two callbacks fire with
        # the same ``time.monotonic()``, the raw stamp would be identical
        # for adjacent frames, corrupting the detector's timeline. Enforce
        # strict spacing of at least one frame_duration between frames. If
        # wall-clock is ahead of extrapolation (real drop / queue backup),
        # trust wall-clock so the detector's gap-fill can kick in.
        if self._last_emitted_ts is not None:
            capture_mono_ts = max(
                wall_clock_ts, self._last_emitted_ts + self.frame_duration
            )
        else:
            capture_mono_ts = wall_clock_ts
        self._last_emitted_ts = capture_mono_ts

        # indata: (frames, channels) float32. We always use mono. Raw levels
        # flow through; final loudness is set by DSP peak-normalize at session
        # close. Per-frame RMS normalization used to live here but caused
        # audible volume pumping without helping STT (Whisper normalizes its
        # own mel spectrogram internally) or VAD/speaker embedding (both are
        # volume-robust).
        mono = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        try:
            loop.call_soon_threadsafe(self.queue.put_nowait, (capture_mono_ts, mono))
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

        # `Stream.latency` returns either a float (InputStream) or a tuple
        # (Stream with both input+output) depending on the sounddevice
        # version. Handle both without risking a TypeError that would leave
        # `_capture_offset` at zero.
        reported_latency = 0.0
        try:
            lat = self._stream.latency
            if isinstance(lat, (tuple, list)):
                reported_latency = float(lat[0])
            else:
                reported_latency = float(lat)
        except Exception:
            reported_latency = 0.0

        self._capture_offset = reported_latency + self.frame_duration

        log.info(
            "mic capture started: device=%s sr=%d latency=%.3fs capture_offset=%.3fs",
            self.device or "default",
            self.sample_rate,
            reported_latency,
            self._capture_offset,
        )

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("mic capture stopped")
