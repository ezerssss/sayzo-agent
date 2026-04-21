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

Wire protocol (new ``SAYZ/v1``): the Swift binary emits each CoreAudio IO
block as a framed record:

    [4 bytes magic "SAYZ"][8 bytes Float64 timestamp][4 bytes UInt32 byte count][N bytes Float32 PCM]

where the timestamp is mach-based monotonic seconds (directly comparable to
Python's ``time.monotonic()`` on macOS). Enqueued frames carry their
hardware-grounded capture time so cross-source alignment with mic capture
stays tight. If a stale audio-tap binary emits raw PCM without a header, we
fall back to monotonic-at-read timing (wider ~50 ms bias) and log a WARN.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import struct
import time
from math import gcd
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

log = logging.getLogger(__name__)

# audio-tap emits mono float32 PCM at this rate.
_NATIVE_RATE = 48_000

# Match the batch size used by the Windows implementation so behaviour
# (latency, resampling quality) is consistent across platforms.
_RESAMPLE_BATCH_FRAMES = 25

# Exit code the Swift binary uses when Audio Capture permission is denied.
_EXIT_PERMISSION_DENIED = 77

# Framing protocol for audio-tap stdout. See main.swift header comment.
_MAGIC = b"SAYZ"
_HEADER_SIZE = 16  # 4 magic + 8 Float64 ts + 4 UInt32 byte count


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

    Spawns the ``audio-tap`` Swift helper as an async subprocess and reads
    framed PCM from its stdout. Resampled frames are pushed into
    ``self.queue`` as ``(capture_mono_ts, frame)`` tuples, matching the
    Windows :class:`system_win.SystemCapture` interface.
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
        self.frame_duration = self.frame_samples / sample_rate
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = asyncio.Queue(maxsize=queue_maxsize)

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
        self._batch_native_duration = self._batch_native_samples / _NATIVE_RATE

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

    async def _read_block(self, stdout: asyncio.StreamReader) -> tuple[float, np.ndarray] | None:
        """Read one framed ``(timestamp, samples)`` block.

        Falls back to legacy raw-PCM mode if the first 4 bytes aren't the
        SAYZ magic — see `_legacy_mode` below. Returns ``None`` on EOF.
        """
        try:
            header = await stdout.readexactly(_HEADER_SIZE)
        except asyncio.IncompleteReadError:
            return None

        magic = bytes(header[:4])
        if magic != _MAGIC:
            # Stale audio-tap binary: emits raw PCM without a header. Log
            # once and hand the buffered bytes to the legacy path.
            log.warning(
                "audio-tap appears to be a stale build (no SAYZ header) — "
                "falling back to monotonic-at-read timing. Cross-source bias "
                "will be wider (~50 ms). Rebuild the Swift binary to fix."
            )
            return await self._legacy_mode(stdout, prebuffered=header)

        ts = struct.unpack("<d", bytes(header[4:12]))[0]
        byte_count = struct.unpack("<I", bytes(header[12:16]))[0]
        if byte_count == 0:
            # Defensive: skip empty payloads rather than hanging on readexactly(0)
            return (ts, np.zeros(0, dtype=np.float32))
        try:
            data = await stdout.readexactly(byte_count)
        except asyncio.IncompleteReadError:
            return None
        samples = np.frombuffer(data, dtype=np.float32).copy()
        return (ts, samples)

    async def _legacy_mode(
        self, stdout: asyncio.StreamReader, prebuffered: bytes
    ) -> tuple[float, np.ndarray] | None:
        """Drain stdout as raw Float32 PCM without timestamp headers.

        Used only when a stale audio-tap binary is detected. We read one
        batch worth of samples and stamp it with ``time.monotonic()`` at
        return minus batch duration — same as the Windows fallback path.
        """
        batch_bytes = self._batch_native_samples * 4
        need = batch_bytes - len(prebuffered)
        try:
            rest = await stdout.readexactly(need) if need > 0 else b""
        except asyncio.IncompleteReadError:
            return None
        data = bytes(prebuffered) + rest
        mono_at_return = time.monotonic()
        ts = mono_at_return - self._batch_native_duration
        samples = np.frombuffer(data, dtype=np.float32).copy()
        # Stay in legacy mode for the rest of the session: patch `_read_block`
        # to skip the magic check. Simpler to re-implement the legacy read
        # loop directly.
        self._read_block = self._read_block_legacy  # type: ignore[assignment]
        return (ts, samples)

    async def _read_block_legacy(
        self, stdout: asyncio.StreamReader
    ) -> tuple[float, np.ndarray] | None:
        """Raw-PCM reader used after a stale-binary detection."""
        batch_bytes = self._batch_native_samples * 4
        try:
            data = await stdout.readexactly(batch_bytes)
        except asyncio.IncompleteReadError:
            return None
        mono_at_return = time.monotonic()
        ts = mono_at_return - self._batch_native_duration
        samples = np.frombuffer(data, dtype=np.float32).copy()
        return (ts, samples)

    async def _reader(self) -> None:
        """Read framed PCM blocks from audio-tap, resample batches of them,
        and push pipeline frames with per-frame capture timestamps."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        # Accumulator for native-rate PCM. The first block's timestamp
        # anchors the batch; per-pipeline-frame stamps are derived by adding
        # `(resampled_offset / target_sr)`.
        accum: list[np.ndarray] = []
        accum_first_mono: float | None = None

        try:
            while True:
                block = await self._read_block(stdout)
                if block is None:
                    log.info("audio-tap stdout closed (process ended)")
                    return
                ts, samples = block
                if samples.size == 0:
                    continue
                if accum_first_mono is None:
                    accum_first_mono = ts
                accum.append(samples)

                total_accum = sum(s.size for s in accum)
                if total_accum < self._batch_native_samples:
                    continue

                # Enough native-rate samples accumulated for one resample
                # batch. Process a whole batch; keep the remainder (and its
                # implied timestamp) for the next iteration.
                full = np.concatenate(accum) if len(accum) > 1 else accum[0]
                batch_native = full[: self._batch_native_samples]
                remainder = full[self._batch_native_samples :]

                batch_first_mono = accum_first_mono

                if remainder.size > 0:
                    # The remainder's first sample is exactly `batch_native_samples`
                    # samples after `accum_first_mono`.
                    accum = [remainder]
                    accum_first_mono = (
                        batch_first_mono
                        + self._batch_native_samples / _NATIVE_RATE
                    )
                else:
                    accum = []
                    accum_first_mono = None

                # Downmix is unnecessary — Swift already delivers mono.
                # Resample the whole batch in one call to avoid boundary
                # artifacts.
                if self._need_resample:
                    resampled = resample_poly(
                        batch_native, self._up, self._down
                    ).astype(np.float32)
                else:
                    resampled = batch_native.astype(np.float32, copy=False)

                # Raw levels flow through; DSP peak-normalize at session close
                # sets final loudness. Per-batch RMS normalize used to live
                # here and caused level jumps at 500 ms batch boundaries.

                # Slice into pipeline frames with per-frame timestamps.
                pos = 0
                while pos + self.frame_samples <= len(resampled):
                    frame = resampled[pos : pos + self.frame_samples]
                    frame_mono = batch_first_mono + (pos / self.sample_rate)
                    pos += self.frame_samples
                    try:
                        self.queue.put_nowait((frame_mono, frame))
                    except asyncio.QueueFull:
                        log.warning("system queue full, dropping frame")

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
