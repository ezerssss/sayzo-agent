"""Smoke tests for the agent ↔ Settings IPC channel.

Coverage focus: the ``OPEN_SETTINGS`` method that drives the user-launch
"open Settings on click" UX. Other methods are integration-tested via the
Settings subprocess; the round-trip here exists so a regression in the
JSON-RPC plumbing or the method-name constant fails fast in unit tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sayzo_agent.gui.settings.ipc import IPCClient, IPCServer, Methods


def test_open_settings_constant_value() -> None:
    # Wire-format constant — pinned so a typo on either side of the channel
    # surfaces here instead of as a silent "unknown method" at runtime.
    assert Methods.OPEN_SETTINGS == "open_settings"


def test_open_settings_round_trip(tmp_path: Path) -> None:
    """A registered OPEN_SETTINGS handler fires on client call and returns ok.

    Uses the real ephemeral-port loopback server on a freshly created
    data_dir so the port file (``ipc.port``) doesn't collide with any
    running agent's port file under the test runner's home dir.
    """
    invocations: list[None] = []

    async def _run() -> dict:
        server = IPCServer(tmp_path)

        def _handler() -> dict:
            invocations.append(None)
            return {"ok": True}

        server.register(Methods.OPEN_SETTINGS, _handler)
        await server.start()

        try:
            client = IPCClient(tmp_path)
            # IPCClient.call is sync; offload to default executor so we
            # don't block the event loop the server is running on.
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, client.call, Methods.OPEN_SETTINGS
            )
        finally:
            await server.stop()

        return result

    result = asyncio.run(_run())
    assert result == {"ok": True}
    assert len(invocations) == 1


def test_open_settings_not_connected_returns_none(tmp_path: Path) -> None:
    """call_quiet swallows IPCNotConnected when no agent is running.

    This is the path a secondary ``sayzo-agent`` takes when nothing
    actually holds the kernel lock — e.g. the previous primary died
    and the IPC server is gone but a stale ``ipc.port`` file remains.
    We must not propagate the connection error — just exit silently.
    """
    # No server started, no port file written — IPCClient.read_port
    # raises IPCNotConnected, which call_quiet must swallow.
    client = IPCClient(tmp_path)
    assert client.call_quiet(Methods.OPEN_SETTINGS) is None


def test_notification_method_constants_pinned() -> None:
    """Wire-format constants — typo guards for the notification plumbing."""
    assert Methods.RELOAD_NOTIFICATION_CONFIG == "reload_notification_config"


def test_reload_notification_config_round_trip(tmp_path: Path) -> None:
    invocations: list[None] = []

    async def _run() -> dict:
        server = IPCServer(tmp_path)

        def _handler() -> dict:
            invocations.append(None)
            return {"reloaded": True}

        server.register(Methods.RELOAD_NOTIFICATION_CONFIG, _handler)
        await server.start()
        try:
            client = IPCClient(tmp_path)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, client.call, Methods.RELOAD_NOTIFICATION_CONFIG
            )
        finally:
            await server.stop()
        return result

    result = asyncio.run(_run())
    assert result == {"reloaded": True}
    assert len(invocations) == 1


def test_quit_agent_constant_value() -> None:
    # Pin the wire constant — a typo on either side becomes silent at runtime.
    assert Methods.QUIT_AGENT == "quit_agent"


def test_quit_agent_round_trip(tmp_path: Path) -> None:
    triggered: list[None] = []

    async def _run() -> dict:
        server = IPCServer(tmp_path)

        def _handler() -> dict:
            triggered.append(None)
            return {"ok": True}

        server.register(Methods.QUIT_AGENT, _handler)
        await server.start()
        try:
            client = IPCClient(tmp_path)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, client.call, Methods.QUIT_AGENT
            )
        finally:
            await server.stop()
        return result

    result = asyncio.run(_run())
    assert result == {"ok": True}
    assert len(triggered) == 1
