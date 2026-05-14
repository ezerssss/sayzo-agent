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

from ._utils import drain_queue as _drain_queue_fn

# pyaudiowpatch + scipy.signal are imported inside ``_run`` instead of at
# module load. Both are only needed once the capture thread spins up (i.e.
# the user has armed); pulling them at import would add ~60 MB and several
# hundred ms to the agent's boot path even when the user never arms.

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
        system_scope: str = "endpoint",  # matches CaptureConfig default since v2.9
        silence_pump_enabled: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_duration = self.frame_samples / sample_rate
        self.device_name = device
        self.system_scope = system_scope  # "arm_app" | "endpoint"
        self.silence_pump_enabled = silence_pump_enabled
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = asyncio.Queue(maxsize=queue_maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._target_pids: tuple[int, ...] = ()
        self._process_loopback = None  # filled when per-app capture wins

    def _snapshot_devices(self, pa) -> list[dict]:
        """One-shot enumeration of every PortAudio device. Cached locally
        in ``_run`` so both the loopback-lookup and the render-lookup
        (for the silence pump) share a single walk."""
        return [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]

    def _find_loopback_device(self, pa, devices: list[dict]) -> dict:
        """Find the WASAPI loopback device for the default speakers."""
        import pyaudiowpatch as pyaudio
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = pa.get_device_info_by_index(
            wasapi_info["defaultOutputDevice"]
        )
        target_name = self.device_name or default_speakers["name"]
        for dev in devices:
            if dev.get("isLoopbackDevice") and target_name in dev["name"]:
                return dev
        raise RuntimeError(
            f"No WASAPI loopback device found for '{target_name}'. "
            f"Available devices: {[d['name'] for d in devices]}"
        )

    def _find_render_device_for_loopback(self, pa, loopback: dict, devices: list[dict]) -> dict:
        """Find the render-side WASAPI device paired with our loopback.

        pyaudiowpatch exposes the same endpoint as TWO devices: a normal
        render device, and a ``[Loopback]`` variant. We want the render
        one so the silence-pump output stream engages the audio engine
        for the same endpoint the loopback is capturing — that's what
        keeps WASAPI delivering continuous loopback packets even when
        no other app is rendering.
        """
        import pyaudiowpatch as pyaudio
        base_name = loopback["name"].replace(" [Loopback]", "").strip()
        for dev in devices:
            if dev.get("isLoopbackDevice"):
                continue
            if int(dev.get("maxOutputChannels", 0)) <= 0:
                continue
            if dev.get("name") == base_name:
                return dev
        # Fallback: WASAPI default output. Same audio engine on Windows so
        # the pump still keeps loopback flowing even if the exact pair is
        # different.
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        return pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

    def _run(self) -> None:
        import pyaudiowpatch as pyaudio
        from scipy.signal import resample_poly
        pa = pyaudio.PyAudio()
        devices = self._snapshot_devices(pa)
        try:
            loopback = self._find_loopback_device(pa, devices)
        except Exception:
            log.error(
                "system capture: failed to find WASAPI loopback device — "
                "the system audio stream will NOT run for this session "
                "(captures will only contain mic audio)",
                exc_info=True,
            )
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
        pump_stream = None
        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=native_rate,
                input=True,
                input_device_index=loopback["index"],
                frames_per_buffer=batch_native_samples,
            )

            # Silence-pump (WASAPI silence-skip workaround): when nothing
            # is rendering on the endpoint, WASAPI delivers no loopback
            # packets and ``stream.read()`` blocks. Opening a tiny silent
            # render stream on the same endpoint keeps the audio engine
            # ticking. See Microsoft / PortAudio #935 / NAudio docs.
            # Open AFTER the loopback; failure is non-fatal.
            if self.silence_pump_enabled:
                try:
                    render_dev = self._find_render_device_for_loopback(pa, loopback, devices)
                    render_channels = max(1, int(render_dev.get("maxOutputChannels", 0)))
                    render_rate = int(render_dev["defaultSampleRate"])
                    pump_buffer_frames = 480  # 10 ms @ 48 kHz
                    silence_buffer = bytes(render_channels * 4 * pump_buffer_frames)

                    def _pump_callback(in_data, frame_count, time_info, status):  # noqa: ANN001
                        # WASAPI may request a different frame_count than our
                        # frames_per_buffer hint; size the slice/extension to it.
                        need = frame_count * render_channels * 4
                        if need <= len(silence_buffer):
                            return (silence_buffer[:need], pyaudio.paContinue)
                        return (silence_buffer + bytes(need - len(silence_buffer)), pyaudio.paContinue)

                    pump_stream = pa.open(
                        format=pyaudio.paFloat32,
                        channels=render_channels,
                        rate=render_rate,
                        output=True,
                        output_device_index=int(render_dev["index"]),
                        frames_per_buffer=pump_buffer_frames,
                        stream_callback=_pump_callback,
                    )
                    pump_stream.start_stream()
                    log.info(
                        "system capture: silence pump active on device=%s "
                        "(rate=%d ch=%d) — WASAPI loopback delivers continuous frames",
                        render_dev["name"], render_rate, render_channels,
                    )
                except Exception:
                    log.warning(
                        "system capture: silence pump open failed; loopback "
                        "may silence-skip when nothing else is playing. "
                        "Set SAYZO_CAPTURE__SYSTEM_SILENCE_PUMP_ENABLED=0 "
                        "to suppress this warning.",
                        exc_info=True,
                    )
                    pump_stream = None

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
                # ``call_soon_threadsafe`` doesn't raise QueueFull — the
                # put_nowait runs on the event loop and any QueueFull
                # surfaces there as an asyncio "Exception in callback"
                # log line. We don't try/except here.
                pos = 0
                while pos + self.frame_samples <= len(samples):
                    frame = samples[pos : pos + self.frame_samples]
                    frame_mono = batch_first_sample_mono + (pos / self.sample_rate)
                    pos += self.frame_samples
                    self._loop.call_soon_threadsafe(
                        self.queue.put_nowait, (frame_mono, frame)
                    )

        except Exception:
            log.exception("system capture loop crashed")
        finally:
            if pump_stream is not None:
                try:
                    pump_stream.stop_stream()
                    pump_stream.close()
                except Exception:
                    log.debug("system capture: silence pump close failed", exc_info=True)
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
        # Drop any frames the producer thread enqueued between the previous
        # ``stop()`` signalling and the thread actually exiting; without
        # this they'd pollute the next session's buffer.
        self._drain_queue()
        # Endpoint scope is the default since v2.9 and the only path
        # Sayzo uses when "Per-app audio capture (beta)" is off.
        # Whitelist auto-arm with the beta toggle ON is the only caller
        # that should pass non-empty target_pids; the hotkey path skips
        # PID computation entirely when the toggle is off (see
        # arm/controller.py::_resolve_hotkey_arm). If we still got PIDs
        # while scope=endpoint, drop them.
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
        # Drain any frames the capture thread enqueued after stop signal
        # but before it actually exited. Mirrors MicCapture.stop's drain.
        self._drain_queue()

    def _drain_queue(self) -> None:
        _drain_queue_fn(self.queue)
