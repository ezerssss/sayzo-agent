"""System audio (loopback) capture via PyAudioWPatch (WASAPI loopback).

Uses the WASAPI loopback device corresponding to the default output speaker.
Captures at the device's native sample rate (typically 48 kHz) and resamples
to the pipeline's target rate (16 kHz) via scipy to avoid quality loss.

To eliminate frame-boundary artifacts, we accumulate a larger chunk of audio
at native rate (multiple pipeline frames worth), resample the whole chunk in
one call, then slice the result into pipeline-sized frames. This avoids the
discontinuities that per-frame resample_poly would produce.

Each enqueued pipeline frame is a ``(capture_mono_ts, pcm)`` tuple.
``capture_mono_ts`` is the ``time.monotonic()`` value corresponding to the
first sample of the frame, derived from PortAudio's ``stream.get_time()``
correlated to ``time.monotonic()`` at stream open. Fallback: monotonic
stamp at batch return minus batch duration (lossier, widens cross-source
bias to ~50 ms).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

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
        *,
        system_scope: str = "arm_app",
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_duration = self.frame_samples / sample_rate
        self.device_name = device
        self.system_scope = system_scope  # "arm_app" | "endpoint"
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = asyncio.Queue(maxsize=queue_maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._target_pids: tuple[int, ...] = ()
        self._process_loopback = None  # filled when per-app capture wins

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
        batch_duration = batch_native_samples / native_rate

        log.info(
            "system capture started: device=%s native_sr=%d target_sr=%d channels=%d "
            "(resample %d/%d, batch=%d frames, batch_dur=%.3fs)",
            loopback["name"],
            native_rate,
            self.sample_rate,
            channels,
            up,
            down,
            _RESAMPLE_BATCH_FRAMES,
            batch_duration,
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

            # Establish a stream-time → monotonic correlation. PortAudio's
            # `stream.get_time()` gives the current stream clock position
            # (hardware-grounded); pairing it with `time.monotonic()` at the
            # same moment lets us convert per-batch stream times into
            # monotonic seconds.
            stream_time_ref: float | None = None
            mono_time_ref: float | None = None
            stamping_mode = "fallback-monotonic"
            try:
                stream_time_ref = float(stream.get_time())
                mono_time_ref = time.monotonic()
                stamping_mode = "stream-time"
            except Exception:
                log.warning(
                    "system capture: PortAudio stream.get_time() unavailable — "
                    "falling back to monotonic-at-return timing (wider ~50 ms bias)."
                )

            log.info("system capture: stamping mode=%s", stamping_mode)

            while not self._stop.is_set():
                raw = stream.read(batch_native_samples, exception_on_overflow=False)
                # Stamp capture time IMMEDIATELY after read returns so the
                # fallback path has minimal extra jitter.
                mono_at_return = time.monotonic()
                if stream_time_ref is not None:
                    try:
                        stream_time_now = float(stream.get_time())
                        # `stream_time_now` corresponds (roughly) to the last
                        # sample in the just-read batch. The first sample was
                        # captured `batch_duration` earlier.
                        batch_first_sample_stream = stream_time_now - batch_duration
                        batch_first_sample_mono = (
                            mono_time_ref
                            + (batch_first_sample_stream - stream_time_ref)
                        )
                    except Exception:
                        batch_first_sample_mono = mono_at_return - batch_duration
                else:
                    batch_first_sample_mono = mono_at_return - batch_duration

                samples = np.frombuffer(raw, dtype=np.float32)

                # Downmix to mono
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)

                # Resample the whole batch at once — no boundary artifacts
                if need_resample:
                    samples = resample_poly(samples, up, down).astype(np.float32)

                # Raw levels flow through; final loudness is set by DSP
                # peak-normalize at session close. A per-batch RMS normalize
                # used to live here and caused audible level jumps at 500 ms
                # batch boundaries whenever the source volume varied.

                # Slice into pipeline-sized frames and enqueue with per-frame
                # monotonic timestamps derived from the batch's first-sample
                # time.
                if self._loop is None:
                    continue
                pos = 0
                while pos + self.frame_samples <= len(samples):
                    frame = samples[pos : pos + self.frame_samples]
                    frame_mono = batch_first_sample_mono + (pos / self.sample_rate)
                    pos += self.frame_samples
                    try:
                        self._loop.call_soon_threadsafe(
                            self.queue.put_nowait, (frame_mono, frame)
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

    async def start(self, *, target_pids: tuple[int, ...] = ()) -> None:
        """Start system-audio capture.

        ``target_pids``: when non-empty, scope the loopback to those
        processes via WASAPI process loopback (requires Windows 10 2004 /
        build 19041). On activation failure (older OS, COM init error,
        PID no longer exists, etc.) we log a warning and fall back to
        endpoint-wide capture so the user doesn't lose audio.

        Empty / None target_pids ⇒ today's endpoint-wide loopback.
        """
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        # Safety-valve opt-out: user set system_scope=endpoint to force the
        # pre-v1.7.0 behavior (e.g. per-app capture misbehaving on their box).
        if self.system_scope == "endpoint" and target_pids:
            log.info(
                "system capture: system_scope=endpoint — ignoring target_pids=%s "
                "and using endpoint-wide loopback per config",
                ",".join(str(p) for p in target_pids),
            )
            target_pids = ()
        self._target_pids = tuple(target_pids)

        if target_pids:
            delegate = await self._try_start_process_loopback(target_pids)
            if delegate is not None:
                # Successfully attached to the process-loopback client.
                # The delegate was constructed with ``queue=self.queue`` so
                # it writes frames into the same queue our external consumer
                # (app._consume) is already draining. We MUST NOT do
                # ``self.queue = delegate.queue`` here — earlier versions did,
                # which left _consume holding a stale reference to the
                # original queue while real audio piled up in the delegate's
                # queue and overflowed (see system_win_process.py docstring).
                self._process_loopback = delegate
                return
            log.warning(
                "system capture: process loopback unavailable for pids=%s — "
                "falling back to endpoint-wide loopback for this session",
                ",".join(str(p) for p in target_pids),
            )

        self._thread = threading.Thread(
            target=self._run, name="system-capture", daemon=True
        )
        self._thread.start()

    async def _try_start_process_loopback(self, target_pids: tuple[int, ...]):
        """Try to spin up + start a ProcessLoopbackCapture.

        Returns the running delegate on success, or None on any error. Kept
        separate so unit tests can mock it to exercise the fallback path
        without needing real WASAPI.
        """
        try:
            from . import system_win_process  # lazy — avoids import on pure-endpoint runs
        except Exception:
            log.debug("system_win_process import failed", exc_info=True)
            return None
        if not system_win_process.is_supported():
            log.info(
                "system capture: Windows build too old for process loopback "
                "(need build %d+); using endpoint fallback",
                system_win_process._MIN_WIN_BUILD,
            )
            return None
        try:
            delegate = system_win_process.ProcessLoopbackCapture(
                target_pids,
                sample_rate=self.sample_rate,
                frame_ms=int(self.frame_duration * 1000),
                # Share our queue so frames flow to the consumer that's
                # already reading from ``self.queue`` (app._consume captured
                # the reference at task creation; reassigning self.queue
                # later wouldn't reach it).
                queue=self.queue,
            )
            await delegate.start()
        except Exception:
            log.warning(
                "system capture: ProcessLoopbackCapture start failed", exc_info=True
            )
            return None
        return delegate

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        delegate = getattr(self, "_process_loopback", None)
        if delegate is not None:
            try:
                await delegate.stop()
            except Exception:
                log.debug("process-loopback delegate stop failed", exc_info=True)
            self._process_loopback = None
