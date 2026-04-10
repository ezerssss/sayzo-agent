"""Upload client interface + implementations."""
from __future__ import annotations

import logging
from typing import Protocol

from .models import ConversationRecord

log = logging.getLogger(__name__)


class UploadClient(Protocol):
    async def upload(self, record: ConversationRecord) -> None: ...


class NoopUploadClient:
    async def upload(self, record: ConversationRecord) -> None:
        log.info("[upload] (noop) record id=%s", record.id)


class AuthenticatedUploadClient:
    """Upload client that uses auth tokens for server requests.

    The actual upload endpoint isn't implemented yet — this verifies
    that auth plumbing works so when a real endpoint exists, it's ready.
    """

    def __init__(self, auth_client) -> None:  # type: auth.AuthenticatedClient
        self._client = auth_client

    async def upload(self, record: ConversationRecord) -> None:
        from .auth.exceptions import AuthenticationRequired

        try:
            token = await self._client.get_token()
            log.info("[upload] (auth-ready) record id=%s token=present", record.id)
        except AuthenticationRequired as e:
            log.warning("[upload] skipped — %s", e)
