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
import sys
import time

import numpy as np

from ._utils import drain_queue as _drain_queue_fn

# ``sounddevice`` is imported inside the methods that touch PortAudio
# (``_resolve_device_index`` + ``MicCapture.start``). Loading it eagerly
# at module import pulls the PortAudio shared library and adds ~6 MB of
# RSS for callers that never actually open a stream — pure-logic tests
# import this module, and the agent's boot path goes through it before
# the user has armed.

log = logging.getLogger(__name__)


def _build_resample_fn(up: int, down: int):
    """Pre-import scipy and bind ``(up, down)`` so the callback hot path
    doesn't pay per-fire import / lookup cost. Hoisting matters here —
    the mic callback fires every 20 ms (50 Hz)."""
    from scipy.signal import resample_poly
    def _resample(mono):
        return resample_poly(mono, up, down).astype(np.float32, copy=False)
    return _resample


def _resolve_device_index(name: str | None) -> int | str | None:
    """Resolve a device name to a numeric PortAudio index.

    Same name under multiple host APIs (e.g. ``"Microphone (2- USB
    Audio Device)"`` exposed by both DirectSound and WASAPI on Windows)
    makes ``sd.InputStream(device=name)`` raise ``ValueError("Multiple
    input devices found")``. Resolving up front to an index and
    preferring WASAPI on Windows / Core Audio on macOS avoids that.

    Returns:
      - ``None`` for ``None``.
      - The original name string if no input device matches (let
        sounddevice raise its own error).
      - A numeric index when at least one host API matches; preferred
        host API wins if multiple match.
    """
    if name is None:
        return None
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return name
    preferred = {"win32": "Windows WASAPI", "darwin": "Core Audio"}.get(sys.platform)
    first_match: int | None = None
    preferred_match: int | None = None
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if dev.get("name") != name:
            continue
        if first_match is None:
            first_match = idx
        host_idx = dev.get("hostapi")
        host = hostapis[host_idx]["name"] if host_idx is not None and 0 <= host_idx < len(hostapis) else ""
        if preferred and host == preferred:
            preferred_match = idx
            break
    if preferred_match is not None:
        return preferred_match
    if first_match is not None:
        return first_match
    return name


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
        # Per-frame resampler used only when the named device rejected our
        # target rate and we fell back to its native rate (typically when
        # OBS / another exclusive-grabbing app pinned the endpoint mix).
        # Set at ``start()`` so the callback's hot path doesn't pay scipy
        # import or factor-computation cost per fire.
        self._resample_fn: callable | None = None

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
        # audible volume pumping without helping anything downstream (Silero
        # VAD is volume-robust; server-side Deepgram does its own gain
        # control on the uploaded Opus).
        mono = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        if self._resample_fn is not None:
            mono = self._resample_fn(mono)
        try:
            loop.call_soon_threadsafe(self.queue.put_nowait, (capture_mono_ts, mono))
        except RuntimeError:
            pass

    async def start(self, *, device: str | None = None) -> None:
        """Open the input stream.

        ``device`` overrides the device picked at construction time. The
        ArmController passes the OS capture device the matched meeting
        app is using (so users with a non-default mic don't get recorded
        from the wrong one); ``None`` keeps the constructor default,
        which is also ``None`` → sounddevice resolves to the OS default
        input. The chosen device is recorded by the log line at the
        bottom of this method so a user reading agent.log can confirm
        which mic is actually open.

        If sounddevice rejects the device (rare — e.g. the device was
        unplugged between the matcher seeing it and us trying to open
        it), we fall back to the OS default and log the failure so the
        user gets *some* audio rather than a hard failure / silent
        capture. The fallback is one-shot per ``start()`` call; we
        don't retry the named device.
        """
        import sounddevice as sd
        self._loop = asyncio.get_running_loop()
        if device is not None:
            self.device = device
        # Defense-in-depth: drain any frames left over from a previous arm
        # cycle. Belt-and-suspenders against the path where stop() left
        # frames in the queue; without this, the consumer would pull
        # 3-minute-old frames as the first input to a new session and
        # the detector's gap-fill would inject minutes of zeros.
        self._drain_queue()
        # Reset the de-aliasing anchor so the first new frame's stamp is
        # set by wall clock, not max(wall, last_old + frame_duration).
        self._last_emitted_ts = None
        self._resample_fn = None
        resolved = _resolve_device_index(self.device)
        try:
            self._stream = self._open_input_stream(sd, self.sample_rate, self.frame_samples, resolved)
        except Exception as exc:
            if self.device is None:
                # Already on the OS default — nothing to fall back to.
                raise
            # Three-tier fallback: 16 kHz on named → native rate on named
            # (+ resample in callback) → 16 kHz on OS default. Native-rate
            # tier exists because another app (OBS, Discord) in exclusive-
            # shared mode pins the endpoint mix to 44.1/48 kHz, so 16 kHz
            # open returns paInvalidSampleRate (-9997). Keeping the user
            # on their intended mic matters when the OS default is the
            # laptop's built-in array mic.
            native_rate, native_resample = self._query_native_rate(sd, resolved)
            if native_rate is not None and native_resample is not None:
                try:
                    log.info(
                        "mic capture: 16 kHz open on device=%r failed (%s); "
                        "retrying at native rate %d Hz with %d/%d in-callback resample",
                        self.device, exc, native_rate,
                        native_resample[0], native_resample[1],
                    )
                    self._resample_fn = _build_resample_fn(*native_resample)
                    self._stream = self._open_input_stream(
                        sd, native_rate, int(native_rate * self.frame_duration), resolved,
                    )
                except Exception as native_exc:
                    log.warning(
                        "mic capture: native-rate retry on device=%r also "
                        "failed (%s); falling back to OS default",
                        self.device, native_exc, exc_info=True,
                    )
                    self._resample_fn = None
                    self.device = None
                    self._stream = self._open_input_stream(sd, self.sample_rate, self.frame_samples, None)
            else:
                log.warning(
                    "mic capture: opening device=%r failed (%s); native-rate "
                    "retry not available, falling back to OS default for "
                    "this session",
                    self.device, exc, exc_info=True,
                )
                self.device = None
                self._stream = self._open_input_stream(sd, self.sample_rate, self.frame_samples, None)

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
            log.warning(
                "mic capture: stream.latency read failed; capture_offset will "
                "default to 0 (cross-source timing alignment may drift)",
                exc_info=True,
            )
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
            # PortAudio can fire one more callback between stream.stop()
            # and the audio thread joining; drain anything that landed.
            self._drain_queue()
            log.info("mic capture stopped")

    def _drain_queue(self) -> None:
        _drain_queue_fn(self.queue)

    def _open_input_stream(self, sd, samplerate: int, blocksize: int, device):
        """Open and start a 1-channel float32 sounddevice InputStream.

        Single source of truth for the three fallback tiers in ``start()``
        (target rate on named device → native rate on named → target rate
        on OS default). Returns the started stream; raises whatever
        ``sd.InputStream`` raises.
        """
        stream = sd.InputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=1,
            dtype="float32",
            device=device,
            callback=self._callback,
        )
        stream.start()
        return stream

    def _query_native_rate(
        self, sd, device_index: int | str | None
    ) -> tuple[int | None, tuple[int, int] | None]:
        """Look up the device's preferred sample rate + compute resample ratios.

        Returns ``(native_rate_hz, (up, down))`` on success, ``(None, None)``
        when the query fails or the device already matches our target rate
        (so no resample retry is useful). ``up/down`` is reduced by gcd to
        keep the resample_poly path cheap (e.g. 48 kHz → 16 kHz becomes
        (1, 3), not (16000, 48000)).
        """
        try:
            info = sd.query_devices(device_index)
            raw = info.get("default_samplerate")
            if raw is None:
                return (None, None)
            native_rate = int(raw)
        except Exception:
            log.debug(
                "mic capture: native-rate query for device=%r failed",
                self.device, exc_info=True,
            )
            return (None, None)
        if native_rate <= 0 or native_rate == self.sample_rate:
            return (None, None)
        from math import gcd
        g = gcd(native_rate, self.sample_rate)
        up = self.sample_rate // g
        down = native_rate // g
        return (native_rate, (up, down))
