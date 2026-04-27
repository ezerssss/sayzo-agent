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
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import click

from .exceptions import AuthenticationCancelled, AuthenticationFailed, PKCEUnavailable
from .models import TokenSet
from .server import AuthServerProtocol

log = logging.getLogger(__name__)

# Auto-close delay shown to the user on the success page. Browsers may
# block window.close() on tabs they didn't open via script; the copy is
# written to work either way ("you can close this tab" stays visible).
_AUTO_CLOSE_SECS = 5


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


_CALLBACK_PAGE_CSS = """\
:root {
  --accent: #2563eb;
  --accent-soft: #eff6ff;
  --success: #059669;
  --success-soft: #ecfdf5;
  --danger: #dc2626;
  --danger-soft: #fef2f2;
  --ink: #1a1a1a;
  --ink-muted: #6b7280;
  --ink-border: #e5e7eb;
  --bg: #ffffff;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.card {
  width: 100%;
  max-width: 440px;
  background: #fff;
  border: 1px solid var(--ink-border);
  border-radius: 14px;
  padding: 40px 32px 32px;
  box-shadow:
    0 1px 2px rgba(15, 23, 42, 0.04),
    0 8px 32px rgba(15, 23, 42, 0.04);
  text-align: center;
  animation: rise 220ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
}
@keyframes rise {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
.logo {
  width: 96px;
  height: 96px;
  margin: 0 auto 18px;
  display: block;
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 14px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.01em;
}
.status-success {
  background: var(--success-soft);
  color: var(--success);
}
.status-error {
  background: var(--danger-soft);
  color: var(--danger);
}
.status svg { display: block; }
h1 {
  margin: 0 0 8px;
  font-size: 22px;
  line-height: 1.25;
  font-weight: 600;
  letter-spacing: -0.015em;
  color: var(--ink);
}
p {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  color: var(--ink-muted);
}
.footer {
  margin-top: 28px;
  padding-top: 20px;
  border-top: 1px solid var(--ink-border);
  font-size: 12px;
  line-height: 1.55;
  color: var(--ink-muted);
}
.countdown {
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  font-weight: 500;
}
.error-code {
  display: inline-block;
  margin-top: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #f3f4f6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--ink);
}
"""


_logo_data_uri_cache: Optional[str] = None


def _logo_data_uri() -> str:
    """Return the Sayzo brand logo as a base64 ``data:`` URI.

    Cached after first call. We embed the logo inline rather than serve
    it from a second endpoint because the localhost HTTP server shuts
    down right after the auth code is exchanged — a deferred image fetch
    can race the shutdown. Inline keeps the page fully self-contained.
    """
    global _logo_data_uri_cache
    if _logo_data_uri_cache is not None:
        return _logo_data_uri_cache

    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    else:
        # auth/pkce.py — climb to repo root.
        base = Path(__file__).resolve().parent.parent.parent / "installer" / "assets"
    logo_path = base / "logo.png"
    try:
        encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        _logo_data_uri_cache = f"data:image/png;base64,{encoded}"
    except OSError:
        # Logo asset missing (unusual but possible in stripped builds).
        # Fall back to an empty string — the <img> just won't render.
        log.warning("logo asset not found at %s; callback page will skip it", logo_path)
        _logo_data_uri_cache = ""
    return _logo_data_uri_cache


def _render_callback_page(*, success: bool, error_code: Optional[str] = None) -> bytes:
    """Render the localhost callback page shown to the user after the OAuth
    redirect. Self-contained: inline CSS + base64-embedded logo so it
    renders cleanly even when the browser is offline."""
    logo_uri = _logo_data_uri()
    logo_tag = (
        f'<img class="logo" src="{logo_uri}" alt="Sayzo" />' if logo_uri else ""
    )

    if success:
        status_svg = (
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<path d="M20 6L9 17l-5-5"/></svg>'
        )
        status_html = (
            f'<div class="status status-success">{status_svg}'
            "<span>Signed in</span></div>"
        )
        title = "You&rsquo;re all set"
        body_html = (
            "<p>You can close this tab and head back to the Sayzo app to "
            "finish setup.</p>"
        )
        footer_html = (
            f'<div class="footer" id="footer">'
            f'This tab will close in <span class="countdown" id="countdown">'
            f"{_AUTO_CLOSE_SECS}</span>s."
            "</div>"
        )
        # Best-effort auto-close. Browsers usually block window.close() on
        # tabs not opened by script; if it's blocked, the copy already
        # tells the user they can close it themselves.
        script = f"""<script>
(function() {{
  var remaining = {_AUTO_CLOSE_SECS};
  var el = document.getElementById('countdown');
  var footer = document.getElementById('footer');
  var timer = setInterval(function() {{
    remaining -= 1;
    if (remaining <= 0) {{
      clearInterval(timer);
      try {{ window.close(); }} catch (e) {{}}
      // If window.close() was blocked, swap in a friendlier message.
      setTimeout(function() {{
        if (footer) {{
          footer.textContent = 'You can close this tab now.';
        }}
      }}, 250);
      return;
    }}
    if (el) el.textContent = String(remaining);
  }}, 1000);
}})();
</script>"""
    else:
        status_svg = (
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<circle cx="12" cy="12" r="10"/>'
            '<line x1="12" y1="8" x2="12" y2="12"/>'
            '<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
        )
        status_html = (
            f'<div class="status status-error">{status_svg}'
            "<span>Sign-in failed</span></div>"
        )
        title = "Sign-in didn&rsquo;t go through"
        body_html = (
            "<p>You can close this tab and try again from Sayzo. "
            "If it keeps failing, check your internet connection or "
            "contact support.</p>"
        )
        if error_code:
            from html import escape

            footer_html = (
                '<div class="footer">Reference: '
                f'<span class="error-code">{escape(error_code)}</span></div>'
            )
        else:
            footer_html = (
                '<div class="footer">'
                "Reference: no code received from the sign-in page."
                "</div>"
            )
        script = ""

    page_title = "Signed in" if success else "Sign-in failed"
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light" />
<title>{page_title} &mdash; Sayzo</title>
<style>{_CALLBACK_PAGE_CSS}</style>
</head>
<body>
  <main class="card" role="main">
    {logo_tag}
    {status_html}
    <h1>{title}</h1>
    {body_html}
    {footer_html}
  </main>
  {script}
</body>
</html>
"""
    return html_doc.encode("utf-8")


def _make_handler_cls(flow: _PkceFlow) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class closed over a single :class:`_PkceFlow`.

    Per-flow handler classes are the cleanest way to avoid class-level
    mutable state: each ``HTTPServer`` we spin up gets its own handler
    class pointing at its own ``_PkceFlow`` state.
    """

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            success = False
            error_code: Optional[str] = None
            if "code" in qs:
                flow.auth_code = qs["code"][0]
                flow.returned_state = qs.get("state", [None])[0]
                success = True
            elif "error" in qs:
                flow.error = qs["error"][0]
                error_code = flow.error
            else:
                flow.error = "no code in callback"
                error_code = flow.error

            body = _render_callback_page(success=success, error_code=error_code)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
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
