"""Microphone capture via sounddevice.

Each frame is enqueued as a ``(capture_mono_ts, pcm)`` tuple where
``capture_mono_ts`` is the ``time.monotonic()`` value corresponding to the
first sample in the frame. We derive it from PortAudio's hardware-stamped
``time_info.inputBufferAdcTime`` (the ADC time of the first sample),
correlated against ``time.monotonic()`` at stream open. This removes the
callback-scheduling jitter that would otherwise make cross-source alignment
unreliable.

Fallback: if ``inputBufferAdcTime`` is zero or non-monotonic (some drivers
don't populate it), stamp with ``time.monotonic()`` at callback entry,
minus the device's reported ``inputLatency``. The fallback is lossier (up
to ~50 ms bias) but still prevents drift.
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import sounddevice as sd

from . import normalize_rms

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
        # Stream-time → monotonic correlation. Captured at stream start, used
        # every callback to convert PortAudio's `inputBufferAdcTime` into
        # `time.monotonic()` seconds.
        self._stream_time_ref: float | None = None
        self._mono_time_ref: float | None = None
        # Fallback: input latency reported by the stream, used when ADC-time
        # isn't available.
        self._input_latency: float = 0.0
        # Whether we logged the clock-source choice yet.
        self._stamping_mode: str = "unknown"

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

        # Stamp capture time at the hardware boundary. Primary: PortAudio's
        # inputBufferAdcTime (ADC time of first sample in indata). Fallback:
        # monotonic() at callback entry minus the reported input latency.
        adc_time = getattr(time_info, "inputBufferAdcTime", 0.0) or 0.0
        if self._stream_time_ref is not None and adc_time > 0.0:
            capture_mono_ts = self._mono_time_ref + (adc_time - self._stream_time_ref)
        else:
            capture_mono_ts = time.monotonic() - self._input_latency

        # indata: (frames, channels) float32. We always use mono.
        mono = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        mono = normalize_rms(mono)
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

        # Grab stream-time / monotonic correlation once, as soon as the
        # stream is running. Some drivers need the first few callbacks to
        # populate `inputBufferAdcTime`; we'll simply detect non-zero ADC
        # time per callback and fall back if absent.
        try:
            mono_ref = time.monotonic()
            stream_ref = float(self._stream.time)
            self._mono_time_ref = mono_ref
            self._stream_time_ref = stream_ref
            self._stamping_mode = "adc-time"
        except Exception:
            self._mono_time_ref = None
            self._stream_time_ref = None
            self._stamping_mode = "fallback-monotonic"

        try:
            self._input_latency = float(self._stream.latency[0])
        except Exception:
            self._input_latency = 0.0

        log.info(
            "mic capture started: device=%s sr=%d mode=%s latency=%.3fs",
            self.device or "default",
            self.sample_rate,
            self._stamping_mode,
            self._input_latency,
        )
        if self._stamping_mode != "adc-time":
            log.warning(
                "mic capture: hardware ADC timestamps unavailable — falling "
                "back to callback-time stamping. Cross-source alignment will "
                "be wider (~50 ms bias)."
            )

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("mic capture stopped")
