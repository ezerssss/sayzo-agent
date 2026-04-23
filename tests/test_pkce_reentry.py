"""Re-entry and cancellation safety for the PKCE flow.

These tests exercise the ``auth.pkce.pkce_flow`` callback-server + poll
loop without opening a browser. A dummy ``AuthServerProtocol`` stands in
for the token-exchange endpoint. ``webbrowser.open`` is monkey-patched
to a no-op so no OS-level browser window is actually opened.

The regression we care about: before the refactor, the callback handler
stored auth_code / state / error on the handler class. A second
``pkce_flow`` call would corrupt an in-flight flow's state. The new flow
uses instance state via a factory — these tests assert that property.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from sayzo_agent.auth.exceptions import (
    AuthenticationCancelled,
    AuthenticationFailed,
)
from sayzo_agent.auth.pkce import pkce_flow


def _cancel_after(cancel: threading.Event, delay_secs: float) -> threading.Thread:
    """Fire ``cancel.set()`` from a background thread after ``delay_secs``.

    In production, cancel is set from a DIFFERENT thread than the one
    running the PKCE flow (the bridge worker vs. the pywebview JS-call
    thread). pkce_flow's poll loop uses a blocking ``Event.wait(0.5)`` on
    the callback-ready event, so the cancel signal must come from another
    thread — an asyncio task in the same loop would block waiting for
    pkce_flow to yield.
    """
    def _go() -> None:
        time.sleep(delay_secs)
        cancel.set()

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


class _DummyServer:
    """Stand-in for AuthServerProtocol. exchange_code is never reached in
    these tests — we cancel or time out before a code arrives."""

    async def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ):
        raise AssertionError("exchange_code should not be reached in cancel tests")

    async def refresh_token(self, refresh_token: str):
        raise NotImplementedError

    async def request_device_code(self):
        raise NotImplementedError

    async def poll_device_code(self, device_code: str):
        raise NotImplementedError


def _port_is_free(port: int) -> bool:
    """True if we can bind port on 127.0.0.1 right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


@pytest.fixture(autouse=True)
def _stub_webbrowser(monkeypatch):
    """Prevent the PKCE flow from opening a real browser."""
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: True)


async def test_cancel_event_aborts_flow():
    cancel = threading.Event()
    _cancel_after(cancel, 0.2)

    with pytest.raises(AuthenticationCancelled):
        await pkce_flow(
            _DummyServer(),
            auth_url="http://example.invalid",
            client_id="cid",
            scopes="s",
            redirect_port=0,  # OS-assigned so we don't fight other tests
            timeout_secs=30,
            cancel_event=cancel,
        )


async def test_cancel_releases_port():
    """After cancellation, the localhost server must be fully shut down
    so the next flow can bind (possibly the same) port."""
    # Try to bind a fixed port for this test. Skip if busy.
    port = 17999
    if not _port_is_free(port):
        pytest.skip(f"test port {port} not free")

    cancel = threading.Event()
    _cancel_after(cancel, 0.15)

    with pytest.raises(AuthenticationCancelled):
        await pkce_flow(
            _DummyServer(),
            auth_url="http://example.invalid",
            client_id="cid",
            scopes="s",
            redirect_port=port,
            timeout_secs=30,
            cancel_event=cancel,
        )

    # Give the threaded HTTP server a brief moment to finish shutdown.
    await asyncio.sleep(0.1)
    assert _port_is_free(port), "pkce_flow did not release its localhost port"


async def test_on_url_ready_emitted_before_poll():
    cancel = threading.Event()
    received: list[str] = []

    def on_url(url: str) -> None:
        received.append(url)

    # Give the flow enough time to construct + fire the URL callback
    # before cancelling.
    _cancel_after(cancel, 0.15)

    with pytest.raises(AuthenticationCancelled):
        await pkce_flow(
            _DummyServer(),
            auth_url="http://auth.example",
            client_id="cid",
            scopes="s",
            redirect_port=0,
            timeout_secs=30,
            cancel_event=cancel,
            on_url_ready=on_url,
        )
    assert len(received) == 1
    assert received[0].startswith("http://auth.example/authorize?")
    assert "code_challenge=" in received[0]
    assert "state=" in received[0]


async def test_two_sequential_flows_do_not_leak_state():
    """Before the refactor, _CallbackHandler had class-level auth_code /
    state, so a cancelled flow could leave stale values that the next flow
    observed. After the refactor, each flow is isolated.

    We verify indirectly: cancel flow 1, start flow 2, confirm flow 2
    still reaches its own timeout (rather than picking up flow 1's never-
    delivered code / error)."""
    cancel1 = threading.Event()
    cancel2 = threading.Event()
    _cancel_after(cancel1, 0.1)

    with pytest.raises(AuthenticationCancelled):
        await pkce_flow(
            _DummyServer(),
            auth_url="http://example.invalid",
            client_id="cid",
            scopes="s",
            redirect_port=0,
            timeout_secs=30,
            cancel_event=cancel1,
        )

    # Second flow with a very short timeout, no cancel. Should time out
    # cleanly rather than picking up any residue from flow 1.
    with pytest.raises(AuthenticationFailed):
        await pkce_flow(
            _DummyServer(),
            auth_url="http://example.invalid",
            client_id="cid",
            scopes="s",
            redirect_port=0,
            timeout_secs=1,
            cancel_event=cancel2,
        )
