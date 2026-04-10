"""System audio (loopback) capture via PyAudioWPatch (WASAPI loopback).

Uses the WASAPI loopback device corresponding to the default output speaker.
Captures at the device's native sample rate (typically 48 kHz) and resamples
to the pipeline's target rate (16 kHz) via scipy to avoid quality loss.

To eliminate frame-boundary artifacts, we accumulate a larger chunk of audio
at native rate (multiple pipeline frames worth), resample the whole chunk in
one call, then slice the result into pipeline-sized frames. This avoids the
discontinuities that per-frame resample_poly would produce.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

from . import normalize_rms

log = logging.getLogger(__name__)

# How many pipeline frames worth of audio to accumulate before resampling.
# Larger = fewer boundary artifacts, but adds latency. 25 frames at 20 ms
# = 500 ms chunks — good tradeoff between quality and responsiveness.
_RESAMPLE_BATCH_FRAMES = 25


class SystemCapture:
    """Captures mono PCM frames from the system output (loopback).

    Runs the blocking PyAudio stream in a background thread and forwards
    resampled frames into an asyncio.Queue on the main loop.
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
        self.device_name = device
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _find_loopback_device(self, pa: pyaudio.PyAudio) -> dict:
        """Find the WASAPI loopback device for the default speakers."""
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = pa.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"]
        )

        if self.device_name:
            target_name = self.device_name
        else:
            target_name = default_speakers["name"]

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if (
                dev.get("isLoopbackDevice")
                and target_name in dev["name"]
            ):
                return dev

        raise RuntimeError(
            f"No WASAPI loopback device found for '{target_name}'. "
            f"Available devices: {[pa.get_device_info_by_index(i)['name'] for i in range(pa.get_device_count())]}"
        )

    def _run(self) -> None:
        pa = pyaudio.PyAudio()
        try:
            loopback = self._find_loopback_device(pa)
        except Exception:
            log.exception("failed to find system loopback device")
            pa.terminate()
            return

        native_rate = int(loopback["defaultSampleRate"])
        channels = max(1, int(loopback["maxInputChannels"]))

        # Resampling parameters
        g = gcd(native_rate, self.sample_rate)
        up = self.sample_rate // g
        down = native_rate // g
        need_resample = native_rate != self.sample_rate

        # We read a large chunk at native rate (multiple pipeline frames),
        # resample the whole chunk once, then slice into pipeline frames.
        # This eliminates the discontinuity artifacts that per-frame
        # resample_poly would produce at every 20 ms boundary.
        native_samples_per_frame = self.frame_samples * down // up
        batch_native_samples = native_samples_per_frame * _RESAMPLE_BATCH_FRAMES

        log.info(
            "system capture started: device=%s native_sr=%d target_sr=%d channels=%d "
            "(resample %d/%d, batch=%d frames)",
            loopback["name"],
            native_rate,
            self.sample_rate,
            channels,
            up,
            down,
            _RESAMPLE_BATCH_FRAMES,
        )

        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=native_rate,
                input=True,
                input_device_index=loopback["index"],
                frames_per_buffer=batch_native_samples,
            )

            while not self._stop.is_set():
                raw = stream.read(batch_native_samples, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)

                # Downmix to mono
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)

                # Resample the whole batch at once — no boundary artifacts
                if need_resample:
                    samples = resample_poly(samples, up, down).astype(np.float32)

                # Normalize the batch to a consistent RMS level so the
                # transcriber sees uniform volume regardless of system volume.
                samples = normalize_rms(samples)

                # Slice into pipeline-sized frames and enqueue
                if self._loop is None:
                    continue
                pos = 0
                while pos + self.frame_samples <= len(samples):
                    frame = samples[pos : pos + self.frame_samples]
                    pos += self.frame_samples
                    try:
                        self._loop.call_soon_threadsafe(
                            self.queue.put_nowait, frame
                        )
                    except asyncio.QueueFull:
                        log.warning("system queue full, dropping frame")

        except Exception:
            log.exception("system capture loop crashed")
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            pa.terminate()
            log.info("system capture stopped")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="system-capture", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
