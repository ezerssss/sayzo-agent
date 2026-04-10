"""PKCE authorization code flow (localhost redirect).

Opens the user's browser to the auth server's authorize endpoint. A
temporary HTTP server on localhost receives the redirect callback with
the authorization code, which is then exchanged for tokens.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import click

from .exceptions import AuthenticationFailed, PKCEUnavailable
from .models import TokenSet
from .server import AuthServerProtocol

log = logging.getLogger(__name__)


def _generate_verifier() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(96)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _bind_server(preferred_port: int) -> HTTPServer:
    """Try to bind on preferred_port, fall back to OS-assigned port."""
    for port in (preferred_port, 0):
        try:
            server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
            return server
        except OSError:
            if port != 0:
                log.debug("port %d busy, trying OS-assigned", preferred_port)
                continue
            raise
    raise PKCEUnavailable("Could not bind any port for PKCE redirect")


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect callback on localhost."""

    # Set by the outer flow before the server starts.
    auth_code: str | None = None
    returned_state: str | None = None
    error: str | None = None
    _ready = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        if "code" in qs:
            _CallbackHandler.auth_code = qs["code"][0]
            _CallbackHandler.returned_state = qs.get("state", [None])[0]
        elif "error" in qs:
            _CallbackHandler.error = qs["error"][0]
        else:
            _CallbackHandler.error = "no code in callback"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Login successful!</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        _CallbackHandler._ready.set()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default stderr logging from BaseHTTPRequestHandler.
        pass


async def pkce_flow(
    server: AuthServerProtocol,
    auth_url: str,
    client_id: str,
    scopes: str,
    redirect_port: int = 17223,
    timeout_secs: int = 120,
) -> TokenSet:
    """Run the PKCE authorization code flow.

    Raises PKCEUnavailable if the localhost server can't start.
    Raises AuthenticationFailed on timeout or error.
    """
    # Reset handler state.
    _CallbackHandler.auth_code = None
    _CallbackHandler.returned_state = None
    _CallbackHandler.error = None
    _CallbackHandler._ready.clear()

    try:
        httpd = _bind_server(redirect_port)
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
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    try:
        opened = webbrowser.open(authorize_url)
        if opened:
            click.echo("Opening browser for login...")
        else:
            click.echo(f"Open this URL in your browser to log in:\n\n  {authorize_url}\n")

        # Wait for the callback.
        got_response = _CallbackHandler._ready.wait(timeout=timeout_secs)
        if not got_response:
            raise AuthenticationFailed(
                f"Login timed out after {timeout_secs}s. Try again."
            )
        if _CallbackHandler.error:
            raise AuthenticationFailed(
                f"Login failed: {_CallbackHandler.error}"
            )
        if _CallbackHandler.returned_state != state:
            raise AuthenticationFailed(
                "State mismatch in callback — possible CSRF attack"
            )
        code = _CallbackHandler.auth_code
        if not code:
            raise AuthenticationFailed("No authorization code received")

        # Exchange code for tokens.
        return await server.exchange_code(code, verifier, redirect_uri)
    finally:
        httpd.shutdown()
        srv_thread.join(timeout=2.0)
