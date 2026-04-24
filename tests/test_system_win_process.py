"""Unit tests for the Windows per-process loopback capture module + its
integration with ``system_win.SystemCapture``.

These tests mock out comtypes / ctypes so they run on any platform without
needing real WASAPI.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# is_supported() Windows-build gate
# ---------------------------------------------------------------------------


class TestIsSupported:
    def test_non_windows_returns_false(self, monkeypatch):
        # Import the module explicitly so we can test its logic on any platform.
        from sayzo_agent.capture import system_win_process

        monkeypatch.setattr(system_win_process.sys, "platform", "linux")
        assert system_win_process.is_supported() is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only check")
    def test_current_windows_build(self):
        from sayzo_agent.capture import system_win_process

        # We can't easily spoof sys.getwindowsversion on a real Windows box,
        # so just make sure the check runs without error and returns a bool.
        result = system_win_process.is_supported()
        assert isinstance(result, bool)

    def test_old_windows_build_returns_false(self, monkeypatch):
        from sayzo_agent.capture import system_win_process

        # Simulate Windows 10 1909 (build 18363 — pre-process-loopback).
        fake_ver = SimpleNamespace(major=10, minor=0, build=18363, platform=2, service_pack="")
        monkeypatch.setattr(system_win_process.sys, "platform", "win32")
        monkeypatch.setattr(system_win_process.sys, "getwindowsversion", lambda: fake_ver)
        assert system_win_process.is_supported() is False

    def test_windows_10_2004_returns_true(self, monkeypatch):
        from sayzo_agent.capture import system_win_process

        fake_ver = SimpleNamespace(major=10, minor=0, build=19041, platform=2, service_pack="")
        monkeypatch.setattr(system_win_process.sys, "platform", "win32")
        monkeypatch.setattr(system_win_process.sys, "getwindowsversion", lambda: fake_ver)
        assert system_win_process.is_supported() is True

    def test_windows_11_returns_true(self, monkeypatch):
        from sayzo_agent.capture import system_win_process

        fake_ver = SimpleNamespace(major=10, minor=0, build=22000, platform=2, service_pack="")
        monkeypatch.setattr(system_win_process.sys, "platform", "win32")
        monkeypatch.setattr(system_win_process.sys, "getwindowsversion", lambda: fake_ver)
        assert system_win_process.is_supported() is True


# ---------------------------------------------------------------------------
# ProcessLoopbackCapture constructor argument validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_requires_at_least_one_pid(self):
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        with pytest.raises(ValueError, match="at least one target PID"):
            ProcessLoopbackCapture(())

    def test_filters_non_positive_pids(self):
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        cap = ProcessLoopbackCapture((1234, 0, -1, 5678))
        assert cap.target_pids == (1234, 5678)

    def test_raises_when_all_pids_invalid(self):
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        with pytest.raises(ValueError, match="no valid PIDs"):
            ProcessLoopbackCapture((0, -1))

    def test_queue_available_after_construction(self):
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        cap = ProcessLoopbackCapture((1234,))
        assert cap.queue is not None
        assert cap.queue.maxsize == 200

    @pytest.mark.asyncio
    async def test_start_rejects_mismatched_pids(self):
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        cap = ProcessLoopbackCapture((1234,))
        with pytest.raises(ValueError, match="target_pids disagrees"):
            await cap.start(target_pids=(5678,))

    @pytest.mark.asyncio
    async def test_start_raises_on_unsupported_platform(self, monkeypatch):
        from sayzo_agent.capture import system_win_process
        from sayzo_agent.capture.system_win_process import ProcessLoopbackCapture

        monkeypatch.setattr(system_win_process, "is_supported", lambda: False)
        cap = ProcessLoopbackCapture((1234,))
        with pytest.raises(RuntimeError, match="requires Windows 10 build"):
            await cap.start()


# ---------------------------------------------------------------------------
# system_win.SystemCapture fallback behavior
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="system_win needs pyaudiowpatch (win-only)")
class TestSystemCaptureFallback:
    """Verify SystemCapture.start delegates to ProcessLoopbackCapture when
    target_pids is non-empty, and falls back to endpoint capture on failure.
    """

    @pytest.mark.asyncio
    async def test_endpoint_path_when_no_target_pids(self, monkeypatch):
        from sayzo_agent.capture import system_win

        cap = system_win.SystemCapture()
        # Short-circuit the blocking _run so we don't actually open WASAPI.
        monkeypatch.setattr(cap, "_run", lambda: None)
        await cap.start()  # no target_pids
        # Thread-based endpoint path must be taken.
        assert cap._thread is not None
        await cap.stop()

    @pytest.mark.asyncio
    async def test_process_loopback_success_uses_delegate_queue(self, monkeypatch):
        from sayzo_agent.capture import system_win

        cap = system_win.SystemCapture()

        # Fake delegate returned by _try_start_process_loopback.
        delegate = MagicMock()
        delegate.queue = MagicMock()
        delegate.stop = AsyncMock()

        async def fake_try(target_pids):
            return delegate

        monkeypatch.setattr(cap, "_try_start_process_loopback", fake_try)

        await cap.start(target_pids=(1234,))
        assert cap.queue is delegate.queue
        assert cap._thread is None  # endpoint thread NOT started

        await cap.stop()
        delegate.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_loopback_failure_falls_back_to_endpoint(self, monkeypatch, caplog):
        from sayzo_agent.capture import system_win

        cap = system_win.SystemCapture()
        monkeypatch.setattr(cap, "_run", lambda: None)

        async def fake_try_fail(target_pids):
            return None  # process-loopback unavailable

        monkeypatch.setattr(cap, "_try_start_process_loopback", fake_try_fail)

        with caplog.at_level("WARNING"):
            await cap.start(target_pids=(1234,))
        assert cap._thread is not None  # endpoint thread DID start
        assert "falling back to endpoint" in caplog.text.lower()

        await cap.stop()

    @pytest.mark.asyncio
    async def test_system_scope_endpoint_forces_endpoint_path(self, monkeypatch, caplog):
        """``SAYZO_CAPTURE__SYSTEM_SCOPE=endpoint`` opt-out: even when
        target_pids is non-empty, we must use the pre-v1.7.0 endpoint
        path and never attempt process loopback."""
        from sayzo_agent.capture import system_win

        cap = system_win.SystemCapture(system_scope="endpoint")
        monkeypatch.setattr(cap, "_run", lambda: None)

        attempted = {"process_loopback": False}

        async def fake_try(target_pids):
            attempted["process_loopback"] = True
            return MagicMock(queue=MagicMock(), stop=AsyncMock())

        monkeypatch.setattr(cap, "_try_start_process_loopback", fake_try)

        with caplog.at_level("INFO"):
            await cap.start(target_pids=(1234,))
        assert not attempted["process_loopback"], (
            "system_scope=endpoint must bypass the process-loopback path entirely"
        )
        assert cap._thread is not None
        assert "system_scope=endpoint" in caplog.text

        await cap.stop()
