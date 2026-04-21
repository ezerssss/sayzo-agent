"""Local token persistence and auto-refresh."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .exceptions import AuthenticationRequired
from .models import TokenSet
from .server import AuthServerProtocol

log = logging.getLogger(__name__)


class TokenStore:
    """Reads/writes auth tokens to a local JSON file.

    get_valid_token() transparently refreshes expired access tokens
    behind an asyncio.Lock so concurrent callers don't double-refresh.
    """

    def __init__(
        self,
        path: Path,
        auth_server: AuthServerProtocol | None = None,
    ) -> None:
        self._path = path
        self._server = auth_server
        self._lock = asyncio.Lock()
        self._tokens: TokenSet | None = None

    def _load(self) -> TokenSet | None:
        if self._tokens is not None:
            return self._tokens
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._tokens = TokenSet(**data)
            return self._tokens
        except Exception:
            log.warning("failed to read auth tokens from %s", self._path)
            return None

    def save(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            tokens.model_dump_json(indent=2), encoding="utf-8"
        )
        # Restrict file permissions on Unix.
        if sys.platform != "win32":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        log.info("auth tokens saved to %s", self._path)

    def clear(self) -> None:
        self._tokens = None
        if self._path.exists():
            self._path.unlink()
            log.info("auth tokens removed")

    def has_tokens(self) -> bool:
        return self._load() is not None

    def invalidate_cache(self) -> None:
        """Drop the in-memory token cache so the next ``get_valid_token()``
        call re-reads from disk.

        Needed when a concurrent ``sayzo-agent login`` subprocess has written
        a fresh token file — otherwise this process's cached copy would stay
        stale until restart, because ``_load()`` only reads from disk the
        first time and then returns the cached value forever.
        """
        self._tokens = None

    async def get_valid_token(self) -> str:
        async with self._lock:
            tokens = self._load()
            if tokens is None:
                raise AuthenticationRequired(
                    "Not authenticated. Run `sayzo-agent login`."
                )
            if not tokens.is_expired:
                return tokens.access_token

            # Try refreshing.
            if self._server is None:
                raise AuthenticationRequired(
                    "Token expired. Run `sayzo-agent login`."
                )
            try:
                new_tokens = await self._server.refresh_token(tokens.refresh_token)
                self.save(new_tokens)
                return new_tokens.access_token
            except Exception:
                log.warning("token refresh failed")
                raise AuthenticationRequired(
                    "Session expired. Run `sayzo-agent login`."
                )
