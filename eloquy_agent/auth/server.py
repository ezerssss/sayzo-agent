"""Server-side auth contract and HTTP implementation.

The HttpAuthServer hits standard OAuth 2.0 endpoints (RFC 7636 PKCE,
RFC 8628 device code). Any compliant provider (Auth0, Supabase, custom)
works — just configure auth_url and client_id.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .exceptions import AuthenticationFailed
from .models import DeviceCodeResponse, TokenSet

log = logging.getLogger(__name__)


class AuthServerProtocol(Protocol):
    async def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenSet: ...

    async def refresh_token(self, refresh_token: str) -> TokenSet: ...

    async def request_device_code(self) -> DeviceCodeResponse: ...

    async def poll_device_code(self, device_code: str) -> TokenSet | None:
        """Return TokenSet on success, None if still pending."""
        ...


def _parse_token_response(data: dict) -> TokenSet:
    expires_in = data.get("expires_in", 3600)
    return TokenSet(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        token_type=data.get("token_type", "Bearer"),
    )


class HttpAuthServer:
    """Concrete auth server client that calls HTTP endpoints."""

    def __init__(self, auth_url: str, client_id: str, scopes: str) -> None:
        # Normalize: strip trailing slash, collapse any double slashes in path.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(auth_url)
        clean_path = "/".join(p for p in parsed.path.split("/") if p)
        if clean_path:
            clean_path = "/" + clean_path
        self._auth_url = urlunparse(parsed._replace(path=clean_path))
        self._client_id = client_id
        self._scopes = scopes

    async def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenSet:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._auth_url}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self._client_id,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
            )
        if resp.status_code != 200:
            log.error("token exchange failed: %s %s", resp.status_code, resp.text)
            raise AuthenticationFailed(f"Token exchange failed ({resp.status_code})")
        return _parse_token_response(resp.json())

    async def refresh_token(self, refresh_token: str) -> TokenSet:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._auth_url}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                },
            )
        if resp.status_code != 200:
            log.error("token refresh failed: %s %s", resp.status_code, resp.text)
            raise AuthenticationFailed(f"Token refresh failed ({resp.status_code})")
        return _parse_token_response(resp.json())

    async def request_device_code(self) -> DeviceCodeResponse:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._auth_url}/device/code",
                data={
                    "client_id": self._client_id,
                    "scope": self._scopes,
                },
            )
        if resp.status_code != 200:
            log.error("device code request failed: %s %s", resp.status_code, resp.text)
            raise AuthenticationFailed(
                f"Device code request failed ({resp.status_code})"
            )
        return DeviceCodeResponse(**resp.json())

    async def poll_device_code(self, device_code: str) -> TokenSet | None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._auth_url}/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": self._client_id,
                },
            )
        if resp.status_code == 200:
            return _parse_token_response(resp.json())

        error = resp.json().get("error", "")
        if error in ("authorization_pending", "slow_down"):
            return None
        raise AuthenticationFailed(f"Device code denied: {error}")
