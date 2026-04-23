"""PKCE authorization code flow (localhost redirect).

Opens the user's browser to the auth server's authorize endpoint. A
temporary HTTP server on localhost receives the redirect callback with
the authorization code, which is then exchanged for tokens.

Re-entry safety: callback state lives on a :class:`_PkceFlow` instance
(not on the handler class), so multiple sequential or concurrent
``pkce_flow()`` calls never corrupt each other. The setup window uses
this to cancel-and-retry without restarting the app.

Recovery hooks exposed to the GUI:

- ``on_url_ready(url)`` — called with the authorize URL the moment it's
  constructed, so the UI can show a "Having trouble? Copy URL" block
  before we call ``webbrowser.open`` (the default browser might not be
  the one the user wants to finish the flow in).
- ``on_tick(seconds_remaining)`` — called roughly every 2 s so the UI
  can render a live countdown.
- ``cancel_event`` — an ``asyncio.Event`` the UI can set to abort the
  flow immediately; the localhost server is released and the function
  raises :class:`AuthenticationCancelled`.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import click

from .exceptions import AuthenticationCancelled, AuthenticationFailed, PKCEUnavailable
from .models import TokenSet
from .server import AuthServerProtocol

log = logging.getLogger(__name__)


def _generate_verifier() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass
class _PkceFlow:
    """Per-invocation state for one PKCE attempt. One instance is bound
    to one temporary HTTPServer + one handler class via
    :func:`_make_handler_cls`."""

    auth_code: Optional[str] = None
    returned_state: Optional[str] = None
    error: Optional[str] = None
    ready: threading.Event = field(default_factory=threading.Event)


def _make_handler_cls(flow: _PkceFlow) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class closed over a single :class:`_PkceFlow`.

    Per-flow handler classes are the cleanest way to avoid class-level
    mutable state: each ``HTTPServer`` we spin up gets its own handler
    class pointing at its own ``_PkceFlow`` state.
    """

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                flow.auth_code = qs["code"][0]
                flow.returned_state = qs.get("state", [None])[0]
            elif "error" in qs:
                flow.error = qs["error"][0]
            else:
                flow.error = "no code in callback"

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab and return to the Sayzo Agent.</p>"
                b"</body></html>"
            )
            flow.ready.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # Silence default stderr logging from BaseHTTPRequestHandler.
            pass

    return _CallbackHandler


def _bind_server(
    handler_cls: type[BaseHTTPRequestHandler], preferred_port: int
) -> HTTPServer:
    """Try to bind on preferred_port, fall back to OS-assigned port."""
    for port in (preferred_port, 0):
        try:
            return HTTPServer(("127.0.0.1", port), handler_cls)
        except OSError:
            if port != 0:
                log.debug("port %d busy, trying OS-assigned", preferred_port)
                continue
            raise
    raise PKCEUnavailable("Could not bind any port for PKCE redirect")


async def pkce_flow(
    server: AuthServerProtocol,
    auth_url: str,
    client_id: str,
    scopes: str,
    redirect_port: int = 17223,
    timeout_secs: int = 90,
    *,
    cancel_event: Optional[threading.Event] = None,
    on_url_ready: Optional[Callable[[str], None]] = None,
    on_tick: Optional[Callable[[int], None]] = None,
) -> TokenSet:
    """Run the PKCE authorization code flow.

    Args:
        server: Auth-server client (exchanges code for tokens).
        auth_url: Authorize endpoint base URL.
        client_id: OAuth client id.
        scopes: Space-separated scope list.
        redirect_port: Preferred localhost port for the redirect listener.
        timeout_secs: Overall deadline. Default 90 s (down from 120 s).
        cancel_event: Optional ``threading.Event``. Setting it aborts the
            flow and raises :class:`AuthenticationCancelled`. A threading
            event is used (not asyncio) because the bridge calling this
            flow lives on a worker thread and signals cancel from the JS
            API call on the GUI thread.
        on_url_ready: Called with the authorize URL right before we open
            the browser — the UI uses this to populate a
            "Having trouble? Copy URL" block for users whose default
            browser isn't the one they want to finish sign-in in.
        on_tick: Called every ~2 s with integer seconds remaining, so
            the UI can render a live countdown.

    Raises:
        PKCEUnavailable: If the localhost server can't start.
        AuthenticationFailed: On timeout or OAuth error.
        AuthenticationCancelled: If ``cancel_event`` fires before the
            flow completes.
    """
    flow = _PkceFlow()
    handler_cls = _make_handler_cls(flow)

    try:
        httpd = _bind_server(handler_cls, redirect_port)
    except (OSError, PKCEUnavailable):
        raise PKCEUnavailable("Cannot start localhost server for PKCE")

    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = _generate_verifier()
    state = secrets.token_urlsafe(32)

    authorize_url = (
        f"{auth_url.rstrip('/')}/authorize"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&scope={scopes}"
        f"&state={state}"
    )

    # Start the callback server in a background thread.
    srv_thread = threading.Thread(
        target=httpd.serve_forever, daemon=True, name="pkce-callback"
    )
    srv_thread.start()

    try:
        # Give the UI the URL BEFORE we try to open the browser — lets it
        # render the copy-URL fallback even if webbrowser.open succeeds
        # but opens in the wrong browser.
        if on_url_ready is not None:
            try:
                on_url_ready(authorize_url)
            except Exception:
                log.debug("[pkce] on_url_ready callback raised", exc_info=True)

        opened = webbrowser.open(authorize_url)
        if opened:
            log.info("browser opened for login")
        else:
            # No callback registered and webbrowser failed — fall back to
            # printing so CLI users still see the URL.
            if on_url_ready is None:
                click.echo(
                    f"Open this URL in your browser to log in:\n\n  {authorize_url}\n"
                )

        # Poll at 0.5 s so cancel events and KeyboardInterrupt propagate
        # quickly and we can emit tick events at ~2 s cadence.
        import time
        deadline = time.monotonic() + timeout_secs
        last_tick_at = 0.0
        got_response = False
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if cancel_event is not None and cancel_event.is_set():
                raise AuthenticationCancelled("Login cancelled by user")

            # Tick callback — integer seconds remaining.
            if on_tick is not None and (now - last_tick_at) >= 2.0:
                try:
                    on_tick(max(0, int(deadline - now)))
                except Exception:
                    log.debug("[pkce] on_tick callback raised", exc_info=True)
                last_tick_at = now

            if flow.ready.wait(timeout=0.5):
                got_response = True
                break

        if not got_response:
            raise AuthenticationFailed(
                f"Login timed out after {timeout_secs}s. Try again."
            )
        if flow.error:
            raise AuthenticationFailed(f"Login failed: {flow.error}")
        if flow.returned_state != state:
            raise AuthenticationFailed(
                "State mismatch in callback — possible CSRF attack"
            )
        code = flow.auth_code
        if not code:
            raise AuthenticationFailed("No authorization code received")

        # Exchange code for tokens.
        return await server.exchange_code(code, verifier, redirect_uri)
    finally:
        httpd.shutdown()
        srv_thread.join(timeout=2.0)
