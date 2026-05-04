"""Authenticated HTTP client wrapper."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .exceptions import AuthenticationRequired
from .store import TokenStore

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)


class AuthenticatedClient:
    """httpx.AsyncClient wrapper that injects auth headers.

    On 401 responses, transparently refreshes the token and retries once.
    """

    def __init__(self, base_url: str, token_store: TokenStore) -> None:
        self._base_url = base_url.rstrip("/")
        self._store = token_store

    @property
    def store(self) -> TokenStore:
        """Underlying TokenStore. Exposed so callers can check token state
        (``has_tokens``, ``invalidate_cache``) without needing a separate
        store instance — both would race against the same on-disk file."""
        return self._store

    async def get_token(self) -> str:
        return await self._store.get_valid_token()

    async def request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        token = await self._store.get_valid_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(base_url=self._base_url) as client:
            resp = await client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 401:
            # Token may have just expired — try one refresh.
            log.debug("got 401, attempting token refresh")
            try:
                token = await self._store.get_valid_token()
            except AuthenticationRequired:
                raise
            headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(base_url=self._base_url) as client:
                resp = await client.request(method, path, headers=headers, **kwargs)
            if resp.status_code == 401:
                raise AuthenticationRequired(
                    "Server rejected credentials. Run `sayzo-agent login`."
                )

        return resp

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)


def make_auth_client(cfg: "Config") -> Optional[AuthenticatedClient]:
    """Build an AuthenticatedClient wired for token refresh.

    Returns ``None`` when the user isn't signed in or the server URL isn't
    configured — callers should treat that as "uploads disabled /
    unauthenticated mode". The returned client owns a TokenStore exposed
    via :attr:`AuthenticatedClient.store` so callers needing token-state
    queries (``has_tokens``, ``invalidate_cache``) don't construct a
    second store that would race against the same file.
    """
    from .server import HttpAuthServer

    probe = TokenStore(cfg.auth_path)
    if not probe.has_tokens() or not cfg.auth.effective_server_url:
        return None

    auth_server = HttpAuthServer(
        cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes,
    )
    store = TokenStore(cfg.auth_path, auth_server=auth_server)
    return AuthenticatedClient(cfg.auth.effective_server_url, store)
