"""Upload client interface + implementations."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

import httpx

from .models import ConversationRecord
from .sink import serialize_record

log = logging.getLogger(__name__)


class UploadClient(Protocol):
    async def upload(self, record: ConversationRecord) -> str | None: ...


class NoopUploadClient:
    async def upload(self, record: ConversationRecord) -> str | None:
        log.info("[upload] (noop) record id=%s", record.id)
        return None


class AuthenticatedUploadClient:
    """Upload captures to the server via multipart POST.

    Raises on any failure — the caller (UploadRetryManager) classifies the
    exception, updates record state, and decides whether to retry. Returns
    the server-assigned capture_id on success.
    """

    def __init__(self, auth_client, captures_dir: Path) -> None:
        from .auth.client import AuthenticatedClient
        self._client: AuthenticatedClient = auth_client
        self._captures_dir = captures_dir

    async def upload(self, record: ConversationRecord) -> str | None:
        audio_path = self._captures_dir / record.id / record.audio_path
        if not audio_path.exists():
            raise FileNotFoundError(f"audio file not found: {audio_path}")

        record_json = json.dumps(serialize_record(record), ensure_ascii=False)
        with open(audio_path, "rb") as audio_file:
            resp = await self._client.post(
                "/api/captures/upload",
                data={"record": record_json},
                files={"audio": ("audio.opus", audio_file, "audio/ogg")},
                timeout=httpx.Timeout(60.0),
            )
        resp.raise_for_status()
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        capture_id = body.get("capture_id") if isinstance(body, dict) else None
        log.info("[upload] success id=%s server_id=%s", record.id, capture_id or "?")
        return capture_id
