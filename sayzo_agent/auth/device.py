"""Device code authorization flow (fallback).

Displays a URL and user code in the terminal. The user opens the URL in
any browser (even on a different device), enters the code, and logs in.
Meanwhile the CLI polls the server until the code is approved.
"""
from __future__ import annotations

import asyncio
import logging

import click
from rich.console import Console
from rich.panel import Panel

from .exceptions import AuthenticationFailed
from .models import TokenSet
from .server import AuthServerProtocol

log = logging.getLogger(__name__)


async def device_code_flow(
    server: AuthServerProtocol,
    timeout_secs: int = 600,
) -> TokenSet:
    """Run the device code authorization flow.

    Raises AuthenticationFailed on timeout, denial, or error.
    """
    resp = await server.request_device_code()

    console = Console()
    url = resp.verification_uri_complete or resp.verification_uri
    console.print(
        Panel(
            f"[bold]Open this URL and enter the code:[/bold]\n\n"
            f"  [link={url}]{url}[/link]\n\n"
            f"  Code: [bold cyan]{resp.user_code}[/bold cyan]",
            title="Sayzo Login",
            expand=False,
        )
    )

    interval = resp.interval
    elapsed = 0
    while elapsed < min(resp.expires_in, timeout_secs):
        await asyncio.sleep(interval)
        elapsed += interval

        try:
            tokens = await server.poll_device_code(resp.device_code)
        except AuthenticationFailed:
            raise
        except Exception:
            log.debug("device code poll error, retrying")
            continue

        if tokens is not None:
            return tokens

    raise AuthenticationFailed("Device code expired. Try again.")
