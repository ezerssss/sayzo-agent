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
    async def upload(self, record: ConversationRecord) -> None: ...


class NoopUploadClient:
    async def upload(self, record: ConversationRecord) -> None:
        log.info("[upload] (noop) record id=%s", record.id)


class AuthenticatedUploadClient:
    """Upload captures to the server via multipart POST."""

    def __init__(self, auth_client, captures_dir: Path) -> None:
        from .auth.client import AuthenticatedClient
        self._client: AuthenticatedClient = auth_client
        self._captures_dir = captures_dir

    async def upload(self, record: ConversationRecord) -> None:
        from .auth.exceptions import AuthenticationRequired

        audio_path = self._captures_dir / record.id / record.audio_path
        if not audio_path.exists():
            log.warning("[upload] skipped id=%s — audio file not found: %s", record.id, audio_path)
            return

        try:
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
            capture_id = body.get("capture_id", "?")
            log.info("[upload] success id=%s server_id=%s", record.id, capture_id)
        except AuthenticationRequired as e:
            log.warning("[upload] skipped id=%s — %s", record.id, e)
        except httpx.HTTPStatusError as e:
            log.warning("[upload] failed id=%s — HTTP %d: %s", record.id, e.response.status_code, e.response.text[:200])
        except httpx.RequestError as e:
            log.warning("[upload] failed id=%s — %s: %s", record.id, type(e).__name__, e)
        except Exception:
            log.exception("[upload] unexpected error for id=%s", record.id)
