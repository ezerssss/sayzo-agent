"""System audio (loopback) capture via the `soundcard` library.

On Windows this uses WASAPI loopback on the default output device.
On macOS/Linux a virtual loopback device (e.g. BlackHole, PulseAudio monitor)
must be selected by name.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import warnings

import numpy as np

# soundcard 0.4.x calls numpy.fromstring on a binary buffer, which raises in
# numpy 2.x ("binary mode of fromstring is removed"). Unconditionally replace
# it with frombuffer (same semantics for the byte-buffer use case).
np.fromstring = np.frombuffer  # type: ignore[attr-defined]

import soundcard as sc
from soundcard import SoundcardRuntimeWarning

# soundcard fires this every time WASAPI reports a timestamp gap. Under
# steady-state load (heavy worker running STT/LLM) it can fire many times per
# second; we already tolerate small loopback gaps, so silence the noise.
warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)

log = logging.getLogger(__name__)


class SystemCapture:
    """Captures mono PCM frames from the system output (loopback).

    Runs the blocking soundcard recorder in a background thread and forwards
    frames into an asyncio.Queue on the main loop.
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

    def _resolve_device(self):
        if self.device_name:
            return sc.get_microphone(self.device_name, include_loopback=True)
        # Default speaker's loopback companion mic
        speaker = sc.default_speaker()
        return sc.get_microphone(speaker.name, include_loopback=True)

    def _run(self) -> None:
        try:
            mic = self._resolve_device()
        except Exception:
            log.exception("failed to open system loopback device")
            return
        log.info("system capture started: device=%s sr=%d", mic.name, self.sample_rate)
        try:
            # NOTE: do NOT pass blocksize=self.frame_samples here. Forcing a
            # 20 ms WASAPI block size makes the loopback ring buffer tiny, so
            # any jitter on this thread (e.g. when the heavy worker starts
            # Whisper/Qwen) causes the OS to drop samples and spam
            # SoundcardRuntimeWarning("data discontinuity in recording"). Let
            # soundcard pick its default (much larger) block size; rec.record
            # still returns exactly numframes samples regardless.
            with mic.recorder(samplerate=self.sample_rate, channels=1) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=self.frame_samples)
                    if data.ndim == 2:
                        data = data[:, 0]
                    frame = data.astype(np.float32, copy=False).copy()
                    if self._loop is None:
                        continue
                    try:
                        self._loop.call_soon_threadsafe(self.queue.put_nowait, frame)
                    except asyncio.QueueFull:
                        log.warning("system queue full, dropping frame")
        except Exception:
            log.exception("system capture loop crashed")
        finally:
            log.info("system capture stopped")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="system-capture", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
