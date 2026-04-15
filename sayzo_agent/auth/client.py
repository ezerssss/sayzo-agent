"""Authenticated HTTP client wrapper."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .exceptions import AuthenticationRequired
from .store import TokenStore

log = logging.getLogger(__name__)


class AuthenticatedClient:
    """httpx.AsyncClient wrapper that injects auth headers.

    On 401 responses, transparently refreshes the token and retries once.
    """

    def __init__(self, base_url: str, token_store: TokenStore) -> None:
        self._base_url = base_url.rstrip("/")
        self._store = token_store

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
