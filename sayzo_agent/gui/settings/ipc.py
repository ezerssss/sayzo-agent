"""JSON-RPC over loopback TCP for the Settings subprocess ↔ live agent.

The Settings window runs in its own process so each instance owns its own
main thread (Cocoa requirement on macOS, plus Tcl thread-affinity workaround
on Windows). Methods that have to mutate live agent state — hotkey
rebinding, token-cache invalidation, mic-holder snapshots — can't reach
into the agent from a subprocess; they need an IPC channel.

Transport choice: ``asyncio.start_server`` over 127.0.0.1 with an
ephemeral port. The original migration plan called for a Unix socket on
macOS and a Windows named pipe on Windows — that route diverges sharply
between platforms (asyncio has no clean named-pipe API; you'd hand-roll
``pywin32`` + thread bridging) for no real gain at this scale. TCP
loopback works identically on both OSes, the same way ``auth/pkce.py``
already brings up a brief HTTP listener for the OAuth redirect, and the
plan's only stated objection (AV / Defender heuristics) targeted loopback
*HTTP*, not raw TCP.

Discovery: the agent writes the chosen port to ``data_dir/ipc.port`` on
startup. The client reads it and connects. Stale port files (agent was
killed, port now belongs to another process) surface as a connection
refused → the client returns ``IPCNotConnected`` and bridge methods
degrade gracefully.

Wire format is newline-delimited JSON. One request per line, one response
per line:

    →  {"id": 1, "method": "ping", "params": {}}\\n
    ←  {"id": 1, "result": "pong"}\\n
    ←  {"id": 1, "error": {"message": "..."}}\\n

Each call opens a fresh connection. Connection pooling is left out
deliberately — Phase 3/4 traffic is bursty and low-volume; if a future
high-frequency path needs it (Meeting Apps mic-holder polling could push
~1 call/2 s), pool it then.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

log = logging.getLogger(__name__)

_PORT_FILENAME = "ipc.port"
_LOOPBACK_HOST = "127.0.0.1"
_REQUEST_TIMEOUT_SECS = 5.0
_CONNECT_TIMEOUT_SECS = 1.5


# IPC method names. Both ``IPCServer.register`` and ``IPCClient.call`` route
# requests by string, so a typo on either side becomes a silent
# ``unknown method`` response at runtime instead of a TypeError at import.
# These constants make the contract explicit.
class Methods:
    PING = "ping"
    INVALIDATE_TOKEN_CACHE = "invalidate_token_cache"
    REBIND_HOTKEY = "rebind_hotkey"
    # Phase 4 — Meeting Apps pane.
    SNAPSHOT_MIC_STATE = "snapshot_mic_state"
    SNAPSHOT_FOREGROUND = "snapshot_foreground"
    RELOAD_DETECTORS = "reload_detectors"
    # Captures pane — surfaces in-flight sessions and lets the Settings
    # subprocess kick the upload retry sweep when the user hits "Try again".
    SNAPSHOT_PROCESSING_CAPTURES = "snapshot_processing_captures"
    NUDGE_UPLOAD_RETRY = "nudge_upload_retry"


class IPCError(Exception):
    """Raised when the IPC server returned an explicit error response."""


class IPCNotConnected(IPCError):
    """Raised when the agent isn't running or the IPC socket couldn't be
    reached. Settings bridge methods catch this and degrade to file-only
    behaviour rather than surfacing it to the user."""


# A registered method may be sync or async; the server normalises both.
Method = Callable[..., Union[Any, Awaitable[Any]]]


def _port_file(data_dir: Path) -> Path:
    return data_dir / _PORT_FILENAME


# ---------------------------------------------------------------------------
# Server (runs in the live agent)
# ---------------------------------------------------------------------------


class IPCServer:
    """Listens on a loopback TCP port and dispatches JSON-RPC requests.

    Usage from the agent's asyncio loop::

        server = IPCServer(cfg.data_dir)
        server.register("ping", lambda: "pong")
        server.register("rebind_hotkey", arm.rebind_hotkey)
        await server.start()
        # ... agent runs ...
        await server.stop()

    Methods can be plain callables or coroutines; the server awaits the
    result if it's awaitable. Method exceptions are caught, logged, and
    returned as ``{"error": {"message": str(e)}}`` — they never crash the
    server loop.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._methods: dict[str, Method] = {}
        self._server: Optional[asyncio.base_events.Server] = None
        self._port: Optional[int] = None

    @property
    def port(self) -> Optional[int]:
        return self._port

    def register(self, name: str, fn: Method) -> None:
        """Register ``name`` as a callable method.

        Re-registering an existing name overwrites silently — convenient
        for tests, and the agent only registers each method once anyway.
        """
        self._methods[name] = fn

    async def start(self) -> None:
        """Bind to an ephemeral port and write it to ``data_dir/ipc.port``."""
        self._server = await asyncio.start_server(
            self._handle_client, host=_LOOPBACK_HOST, port=0,
        )
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock is None:
            raise RuntimeError("IPC server failed to bind a socket")
        self._port = sock.getsockname()[1]

        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            _port_file(self._data_dir).write_text(str(self._port), encoding="utf-8")
        except OSError:
            log.warning("[ipc] failed to write port file", exc_info=True)

        log.info("[ipc] listening on %s:%d", _LOOPBACK_HOST, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                log.debug("[ipc] wait_closed raised", exc_info=True)
            self._server = None

        # Best-effort cleanup of the port file. Stale files are harmless
        # (clients see connection refused and treat as not-connected) but
        # leaving them around clutters the data dir across restarts.
        try:
            _port_file(self._data_dir).unlink(missing_ok=True)
        except OSError:
            pass

        self._port = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    return
                response = await self._dispatch_line(line)
                writer.write(response + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.warning("[ipc] handler error for %s", peer, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_line(self, line: bytes) -> bytes:
        # Parsing failures get id=null because we couldn't even read the id.
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return self._make_error(None, f"malformed request: {e}")

        if not isinstance(req, dict):
            return self._make_error(None, "request must be a JSON object")

        req_id = req.get("id")
        method_name = req.get("method")
        params = req.get("params") or {}
        if not isinstance(method_name, str):
            return self._make_error(req_id, "missing or non-string 'method'")
        if not isinstance(params, dict):
            return self._make_error(req_id, "'params' must be an object")

        method = self._methods.get(method_name)
        if method is None:
            return self._make_error(req_id, f"unknown method: {method_name}")

        try:
            result = method(**params)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:
            log.warning("[ipc] method %r raised", method_name, exc_info=True)
            return self._make_error(req_id, str(e))

        return json.dumps({"id": req_id, "result": result}).encode("utf-8")

    @staticmethod
    def _make_error(req_id: Any, message: str) -> bytes:
        return json.dumps(
            {"id": req_id, "error": {"message": message}}
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Client (runs in the Settings subprocess)
# ---------------------------------------------------------------------------


class IPCClient:
    """Synchronous JSON-RPC client over the agent's loopback port.

    Sync because the Settings bridge methods are called from pywebview's
    JS bridge thread, which is not an asyncio loop. Each ``call`` opens a
    fresh socket using stdlib ``socket`` (no asyncio glue needed for the
    client side).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._next_id = 0
        self._cached_port: Optional[int] = None

    def _read_port(self) -> int:
        """Return the agent's IPC port, cached across calls.

        The port file is read from disk at most once per healthy connection
        — re-reading on every call would add 10–20 µs of stat+read+parse
        overhead in Phase 4's mic-holder polling. Cache is invalidated by
        ``call`` on ``ConnectionRefusedError`` so an agent that restarted
        and bound a different port is picked up automatically.
        """
        if self._cached_port is not None:
            return self._cached_port
        path = _port_file(self._data_dir)
        try:
            self._cached_port = int(path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError) as e:
            raise IPCNotConnected(f"agent IPC port file unreadable: {e}")
        return self._cached_port

    def call(self, method: str, **params: Any) -> Any:
        """Send a request and return the result (or raise ``IPCError``).

        Raises ``IPCNotConnected`` if the agent isn't reachable. Bridge
        callers should catch that specifically and degrade gracefully —
        for example, ``invalidate_token_cache`` is a no-op if the agent
        isn't running because there's no cache to invalidate.
        """
        import socket

        port = self._read_port()
        self._next_id += 1
        req_id = self._next_id
        request = json.dumps(
            {"id": req_id, "method": method, "params": params},
        ).encode("utf-8") + b"\n"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT_SECS)
        try:
            try:
                sock.connect((_LOOPBACK_HOST, port))
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                # Drop the cached port — the agent may have restarted and
                # bound a new one. Next call's _read_port() picks up fresh.
                self._cached_port = None
                raise IPCNotConnected(f"agent not reachable on :{port}: {e}")

            sock.settimeout(_REQUEST_TIMEOUT_SECS)
            sock.sendall(request)

            buf = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break
        finally:
            try:
                sock.close()
            except OSError:
                pass

        line, _, _ = bytes(buf).partition(b"\n")
        if not line:
            raise IPCError("empty response from agent")
        try:
            resp = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise IPCError(f"malformed response: {e}")

        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
            raise IPCError(msg)
        return resp.get("result")

    def call_quiet(self, method: str, **params: Any) -> Any:
        """Like ``call`` but swallows ``IPCNotConnected`` and returns None.

        Convenience for fire-and-forget nudges (cache invalidation,
        notification reloads) where we don't want every bridge method to
        re-implement the same try/except.
        """
        try:
            return self.call(method, **params)
        except IPCNotConnected:
            log.debug("[ipc] %s skipped — agent not reachable", method)
            return None
