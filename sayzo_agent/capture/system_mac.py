"""System audio (loopback) capture via a CoreAudio Process Taps Swift helper.

macOS has no WASAPI-style loopback API.  We spawn a small Swift binary
(``audio-tap``) that uses the CoreAudio Process Taps API (macOS 14.4+) to
capture all system audio and pipes raw mono float32 PCM at 48 kHz to stdout.
This module reads that pipe, resamples to the pipeline target rate, and
pushes normalised frames into the same asyncio queue interface that
:class:`system_win.SystemCapture` provides.

The Swift binary must be compiled separately on a Mac::

    cd sayzo_agent/capture/audio-tap
    swiftc -O -o audio-tap main.swift \\
        -framework CoreAudio -framework AudioToolbox -framework AVFoundation

Binary lookup order:
1. Same directory as this file (package-data install).
2. ``audio-tap`` anywhere on ``PATH``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from . import normalize_rms

log = logging.getLogger(__name__)

# audio-tap emits mono float32 PCM at this rate.
_NATIVE_RATE = 48_000

# Match the batch size used by the Windows implementation so behaviour
# (latency, resampling quality) is consistent across platforms.
_RESAMPLE_BATCH_FRAMES = 25

# Exit code the Swift binary uses when Audio Capture permission is denied.
_EXIT_PERMISSION_DENIED = 77


def _find_audio_tap() -> str:
    """Locate the ``audio-tap`` binary, raising FileNotFoundError if absent."""
    # 1. Next to this file (package_data install / dev checkout).
    here = Path(__file__).resolve().parent / "audio-tap" / "audio-tap"
    if here.is_file() and os.access(here, os.X_OK):
        return str(here)

    # 2. On PATH.
    on_path = shutil.which("audio-tap")
    if on_path is not None:
        return on_path

    raise FileNotFoundError(
        "audio-tap binary not found.  Compile it on macOS with:\n"
        "  cd sayzo_agent/capture/audio-tap\n"
        "  swiftc -O -o audio-tap main.swift "
        "-framework CoreAudio -framework AudioToolbox "
        "-framework AVFoundation"
    )


class SystemCapture:
    """Captures mono PCM frames from all system audio via CoreAudio Process Taps.

    Spawns the ``audio-tap`` Swift helper as an async subprocess and reads raw
    PCM from its stdout.  Resampled frames are pushed into ``self.queue``
    exactly like the Windows :class:`system_win.SystemCapture`.
    """

    def __init__(
        self,
        sample_rate: int = 16_000,
        frame_ms: int = 20,
        device: str | None = None,
        queue_maxsize: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_maxsize)

        if device is not None:
            log.warning(
                "device=%r ignored on macOS — CoreAudio Process Taps capture "
                "all system audio, no per-device selection",
                device,
            )

        # Resampling parameters (48 kHz → target).
        g = gcd(_NATIVE_RATE, sample_rate)
        self._up = sample_rate // g
        self._down = _NATIVE_RATE // g
        self._need_resample = _NATIVE_RATE != sample_rate

        native_samples_per_frame = self.frame_samples * self._down // self._up
        self._batch_native_samples = native_samples_per_frame * _RESAMPLE_BATCH_FRAMES

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        binary = _find_audio_tap()
        log.info("starting audio-tap: %s", binary)

        self._proc = await asyncio.create_subprocess_exec(
            binary,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Give the process a moment to either start streaming or fail fast
        # (e.g. permission denied → exit code 77).
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            # Still running after 2 s — that's the happy path.
            pass
        else:
            # Process exited early.
            code = self._proc.returncode
            stderr_bytes = b""
            if self._proc.stderr:
                stderr_bytes = await self._proc.stderr.read()
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            if code == _EXIT_PERMISSION_DENIED:
                raise PermissionError(
                    "Audio Capture permission denied.  Grant it in:\n"
                    "  System Settings → Privacy & Security → Audio Capture\n"
                    "Then restart the agent."
                )
            raise RuntimeError(
                f"audio-tap exited immediately with code {code}: {stderr_text}"
            )

        self._reader_task = asyncio.create_task(self._reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())

        log.info(
            "system capture started: native_sr=%d target_sr=%d "
            "(resample %d/%d, batch=%d frames)",
            _NATIVE_RATE,
            self.sample_rate,
            self._up,
            self._down,
            _RESAMPLE_BATCH_FRAMES,
        )

    async def stop(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
            log.info("audio-tap stopped (code %d)", self._proc.returncode)

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._proc = None
        self._reader_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _reader(self) -> None:
        """Read raw float32 PCM from audio-tap stdout, resample, enqueue."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        # Bytes per batch: mono float32 at native rate.
        batch_bytes = self._batch_native_samples * 4

        try:
            while True:
                data = await stdout.readexactly(batch_bytes)
                samples = np.frombuffer(data, dtype=np.float32).copy()

                if self._need_resample:
                    samples = resample_poly(
                        samples, self._up, self._down
                    ).astype(np.float32)

                samples = normalize_rms(samples)

                pos = 0
                while pos + self.frame_samples <= len(samples):
                    frame = samples[pos : pos + self.frame_samples]
                    pos += self.frame_samples
                    try:
                        self.queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        log.warning("system queue full, dropping frame")

        except asyncio.IncompleteReadError:
            log.info("audio-tap stdout closed (process ended)")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system capture reader crashed")

    async def _stderr_reader(self) -> None:
        """Forward audio-tap stderr to the Python logger."""
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr

        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                log.warning("[audio-tap] %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stderr reader crashed")
