"""Replay capture — feeds a pre-recorded audio file through the pipeline
as if it were live mic + system audio.

Stereo files: channel 0 → mic, channel 1 → system.
Mono files: same audio feeds both mic and system queues.

Usage:
    sayzo-agent replay conversation.wav
    sayzo-agent replay conversation.wav --speed 4
    sayzo-agent replay conversation.wav --speed 0   # as fast as possible
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import av
import numpy as np

log = logging.getLogger(__name__)


def _decode_stream(stream, container, target_sr: int) -> np.ndarray:
    """Decode a single audio stream to a mono float32 array at target_sr."""
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=target_sr)
    chunks: list[np.ndarray] = []
    for frame in container.decode(stream):
        resampled = resampler.resample(frame)
        for r in (resampled if isinstance(resampled, list) else [resampled]):
            arr = r.to_ndarray()  # (1, samples)
            chunks.append(arr[0])
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32)


def load_audio(
    path: str | Path, target_sr: int = 16000, channel: str = "both"
) -> tuple[np.ndarray, np.ndarray]:
    """Load an audio file, resample, and split into (mic, system) float32 arrays.

    Returns two 1-D float32 arrays normalised to [-1, 1].

    Multi-track files (e.g. OBS with separate mic/desktop tracks):
        track 0 = mic, track 1 = system.
    Single-track stereo: ch0=mic, ch1=system.
    Single-track mono: routed by *channel*:
        "both"   — same audio to mic and system (default)
        "mic"    — audio to mic, silence to system
        "system" — silence to mic, audio to system
    """
    container = av.open(str(path))
    n_audio_streams = len(container.streams.audio)

    if n_audio_streams >= 2:
        # Multi-track: track 0 = mic, track 1 = system (e.g. OBS recording)
        mic = _decode_stream(container.streams.audio[0], container, target_sr)
        # Must reopen because PyAV can only decode one stream per iteration
        container.close()
        container = av.open(str(path))
        sys_ = _decode_stream(container.streams.audio[1], container, target_sr)
        container.close()
        # Pad shorter track with silence so both are the same length
        if len(mic) != len(sys_):
            max_len = max(len(mic), len(sys_))
            mic = np.pad(mic, (0, max_len - len(mic)))
            sys_ = np.pad(sys_, (0, max_len - len(sys_)))
        log.info(
            "loaded %s: %.1fs, %d audio tracks (track0=mic, track1=system)",
            Path(path).name,
            len(mic) / target_sr,
            n_audio_streams,
        )
        return mic, sys_

    # Single audio stream — check channels
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=target_sr)
    chunks: list[np.ndarray] = []
    for frame in container.decode(stream):
        resampled = resampler.resample(frame)
        for r in (resampled if isinstance(resampled, list) else [resampled]):
            chunks.append(r.to_ndarray())  # (channels, samples)
    container.close()

    if not chunks:
        empty = np.zeros(0, dtype=np.float32)
        return empty, empty

    audio = np.concatenate(chunks, axis=1)  # (channels, total_samples)

    if audio.shape[0] >= 2:
        mic = audio[0].astype(np.float32)
        sys_ = audio[1].astype(np.float32)
        routing = "stereo"
    else:
        mono = audio[0].astype(np.float32)
        silence = np.zeros_like(mono)
        if channel == "mic":
            mic, sys_ = mono, silence
        elif channel == "system":
            mic, sys_ = silence, mono
        else:  # "both"
            mic, sys_ = mono, mono.copy()
        routing = channel

    duration = max(len(mic), len(sys_)) / target_sr
    log.info(
        "loaded %s: %.1fs, %d ch, routing=%s → mic=%.1fs sys=%.1fs",
        Path(path).name,
        duration,
        audio.shape[0],
        routing,
        len(mic) / target_sr,
        len(sys_) / target_sr,
    )
    return mic, sys_


def save_wav(audio: np.ndarray, sample_rate: int, path: str | Path) -> None:
    """Write a float32 array to a 16-bit WAV file for debugging."""
    import wave

    pcm16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    log.info("saved %s (%.1fs, %d Hz)", path, len(audio) / sample_rate, sample_rate)


class ReplayCapture:
    """Drop-in replacement for MicCapture/SystemCapture that feeds frames
    from a pre-loaded float32 array."""

    def __init__(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        speed: float = 1.0,
        queue_maxsize: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=queue_maxsize)
        self._audio = audio
        self._speed = speed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._done = asyncio.Event()

    @property
    def done(self) -> asyncio.Event:
        """Signals when all frames have been fed."""
        return self._done

    def _run(self) -> None:
        n_samples = len(self._audio)
        frame_dur = self.frame_samples / self.sample_rate
        sleep_dur = frame_dur / self._speed if self._speed > 0 else 0.0
        pos = 0
        frames_fed = 0

        while pos < n_samples and not self._stop.is_set():
            end = pos + self.frame_samples
            chunk = self._audio[pos:end]
            if len(chunk) < self.frame_samples:
                # Pad final frame with silence
                chunk = np.pad(chunk, (0, self.frame_samples - len(chunk)))
            pos = end
            frames_fed += 1

            if self._loop is None:
                continue
            try:
                self._loop.call_soon_threadsafe(self.queue.put_nowait, chunk)
            except asyncio.QueueFull:
                # Back-pressure: wait a bit and retry
                import time

                time.sleep(0.01)
                try:
                    self._loop.call_soon_threadsafe(self.queue.put_nowait, chunk)
                except asyncio.QueueFull:
                    pass

            if sleep_dur > 0:
                self._stop.wait(timeout=sleep_dur)

        duration = n_samples / self.sample_rate
        log.info(
            "replay finished: %d frames (%.1fs) at %.1fx speed",
            frames_fed,
            duration,
            self._speed,
        )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._done.set)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._done.clear()
        self._thread = threading.Thread(
            target=self._run, name="replay-capture", daemon=True
        )
        self._thread.start()
        log.info(
            "replay capture started: %.1fs of audio at %.1fx speed",
            len(self._audio) / self.sample_rate,
            self._speed,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
