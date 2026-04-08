"""Upload client interface + no-op default implementation."""
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
