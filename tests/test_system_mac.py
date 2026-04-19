"""Unit tests for the macOS system-capture Python wrapper.

These tests run on any platform — they mock the ``audio-tap`` subprocess so no
Mac or Swift binary is needed.
"""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

# Import the module directly (not via __init__ dispatch) so we can test it on
# any platform without triggering the platform guard.
from sayzo_agent.capture.system_mac import (
    SystemCapture,
    _EXIT_PERMISSION_DENIED,
    _NATIVE_RATE,
    _find_audio_tap,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pcm_bytes(n_samples: int, freq: float = 440.0) -> bytes:
    """Generate ``n_samples`` of mono float32 PCM at ``_NATIVE_RATE``."""
    t = np.arange(n_samples, dtype=np.float32) / _NATIVE_RATE
    tone = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return tone.tobytes()


def _framed(pcm: bytes, timestamp: float) -> bytes:
    """Wrap PCM in the SAYZ/v1 protocol: magic + Float64 ts + UInt32 bytecount + PCM."""
    return b"SAYZ" + struct.pack("<d", timestamp) + struct.pack("<I", len(pcm)) + pcm


class FakeStdout:
    """Simulates an asyncio subprocess stdout that yields predetermined data."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            raise asyncio.IncompleteReadError(b"", n)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk

    async def readline(self) -> bytes:
        return b""

    async def read(self) -> bytes:
        rest = self._data[self._pos :]
        self._pos = len(self._data)
        return rest


class FakeProc:
    """Simulates an asyncio subprocess."""

    def __init__(
        self,
        stdout_data: bytes = b"",
        exit_code: int | None = None,
    ) -> None:
        self.stdout = FakeStdout(stdout_data)
        self.stderr = FakeStdout(b"")
        self._exit_code = exit_code
        self.returncode: int | None = None
        self._wait_called = False
        self._signal_sent: int | None = None
        self._killed = asyncio.Event()

    async def wait(self) -> int:
        if self._exit_code is not None and not self._wait_called:
            self._wait_called = True
            self.returncode = self._exit_code
            return self._exit_code
        if self.returncode is not None:
            return self.returncode
        # Simulate a long-running process — but wake up when killed/signalled.
        await self._killed.wait()
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self._signal_sent = sig
        self.returncode = -sig
        self._killed.set()

    def kill(self) -> None:
        self.returncode = -9
        self._killed.set()


class FastExitProc(FakeProc):
    """Process that exits immediately (simulates permission denied, etc.)."""

    async def wait(self) -> int:
        assert self._exit_code is not None
        self.returncode = self._exit_code
        return self._exit_code


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def cap() -> SystemCapture:
    """Default capture at 16 kHz, 20 ms frames."""
    return SystemCapture(sample_rate=16_000, frame_ms=20)


class TestFindBinary:
    def test_raises_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "sayzo_agent.capture.system_mac.Path",
            lambda *a: tmp_path / "nonexistent",
        )
        monkeypatch.setattr(
            "sayzo_agent.capture.system_mac.shutil.which",
            lambda _: None,
        )
        with pytest.raises(FileNotFoundError, match="audio-tap binary not found"):
            _find_audio_tap()


class TestStartPermissionDenied:
    @pytest.mark.asyncio
    async def test_exit_77_raises_permission_error(self, cap):
        proc = FastExitProc(exit_code=_EXIT_PERMISSION_DENIED)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            with pytest.raises(PermissionError, match="Audio Capture"):
                await cap.start()

    @pytest.mark.asyncio
    async def test_unexpected_exit_raises_runtime_error(self, cap):
        proc = FastExitProc(exit_code=1)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            with pytest.raises(RuntimeError, match="audio-tap exited immediately"):
                await cap.start()


class TestReader:
    @pytest.mark.asyncio
    async def test_frames_arrive_in_queue(self, cap):
        """Feed one full batch of SAYZ-framed PCM and verify (ts, frame)
        tuples land in the queue."""
        pcm = _make_pcm_bytes(cap._batch_native_samples)
        framed = _framed(pcm, timestamp=1234.5)
        proc = FakeProc(stdout_data=framed)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            await cap.start()

            # Give the reader task a tick to process.
            await asyncio.sleep(0.1)

            items = []
            while not cap.queue.empty():
                items.append(cap.queue.get_nowait())

            assert len(items) > 0
            for ts, frame in items:
                assert isinstance(ts, float)
                assert frame.shape == (cap.frame_samples,)
                assert frame.dtype == np.float32

            await cap.stop()

    @pytest.mark.asyncio
    async def test_frame_timestamps_match_header(self, cap):
        """Per-frame ``capture_mono_ts`` must derive from the batch header's
        timestamp + the frame's offset within the resampled batch."""
        pcm = _make_pcm_bytes(cap._batch_native_samples)
        header_ts = 9999.125
        framed = _framed(pcm, timestamp=header_ts)
        proc = FakeProc(stdout_data=framed)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            await cap.start()
            await asyncio.sleep(0.1)

            items = []
            while not cap.queue.empty():
                items.append(cap.queue.get_nowait())

            assert len(items) >= 2
            # First frame's timestamp equals header timestamp.
            assert abs(items[0][0] - header_ts) < 1e-6
            # Second frame's timestamp is exactly one frame_duration later.
            expected_ts = header_ts + cap.frame_duration
            assert abs(items[1][0] - expected_ts) < 1e-6

            await cap.stop()

    @pytest.mark.asyncio
    async def test_resampled_frame_size(self, cap):
        """Verify that resampled frames have exactly frame_samples samples."""
        pcm = _make_pcm_bytes(cap._batch_native_samples)
        framed = _framed(pcm, timestamp=0.0)
        proc = FakeProc(stdout_data=framed)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            await cap.start()
            await asyncio.sleep(0.1)

            _ts, frame = cap.queue.get_nowait()
            # 16 kHz * 20 ms = 320 samples per frame
            assert frame.shape == (320,)

            await cap.stop()

    @pytest.mark.asyncio
    async def test_legacy_binary_falls_back_with_warning(self, cap, caplog):
        """A stale audio-tap binary emits raw PCM without the SAYZ header.
        The reader should log a WARN and fall back to monotonic-at-read
        stamping rather than crashing."""
        # Raw PCM — no SAYZ prefix.
        pcm = _make_pcm_bytes(cap._batch_native_samples)
        proc = FakeProc(stdout_data=pcm)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ), caplog.at_level("WARNING"):
            await cap.start()
            await asyncio.sleep(0.1)

            items = []
            while not cap.queue.empty():
                items.append(cap.queue.get_nowait())

            assert len(items) > 0, "fallback path should still produce frames"
            for ts, frame in items:
                assert isinstance(ts, float)
                assert frame.shape == (cap.frame_samples,)
            assert "stale build" in caplog.text.lower()

            await cap.stop()

    @pytest.mark.asyncio
    async def test_queue_full_drops_frames(self):
        """When the queue is full, extra frames are dropped, not blocking."""
        cap = SystemCapture(sample_rate=16_000, frame_ms=20, queue_maxsize=2)

        # Generate enough data for many frames (3 batches worth), each properly framed.
        framed = b""
        for i in range(3):
            pcm = _make_pcm_bytes(cap._batch_native_samples)
            framed += _framed(pcm, timestamp=float(i))
        proc = FakeProc(stdout_data=framed)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            await cap.start()
            await asyncio.sleep(0.2)

            # Queue should be at its max.
            assert cap.queue.qsize() <= 2

            await cap.stop()


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_sends_sigterm(self, cap):
        import signal as sig_mod

        pcm = _make_pcm_bytes(cap._batch_native_samples * 10)
        proc = FakeProc(stdout_data=pcm)

        with patch(
            "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
        ), patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ):
            await cap.start()
            await asyncio.sleep(0.05)
            await cap.stop()

            assert proc._signal_sent == sig_mod.SIGTERM


class TestDeviceWarning:
    def test_device_param_logged_as_warning(self, caplog):
        with caplog.at_level("WARNING"):
            SystemCapture(device="some-device")
        assert "ignored on macOS" in caplog.text
