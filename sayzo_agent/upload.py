"""Upload client interface + implementations."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

import httpx

from . import __version__
from .models import ConversationRecord
from .sink import AUDIO_FILENAME, serialize_record_for_upload

log = logging.getLogger(__name__)


def parse_json_body(resp: httpx.Response) -> dict | None:
    """Best-effort JSON-body extraction from an httpx response.

    Returns the parsed dict, or ``None`` if the response wasn't JSON, the
    body didn't parse, or the top level wasn't an object. Never raises —
    callers downgrade missing bodies to "no info available" rather than
    surfacing a parse error.
    """
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        parsed = resp.json()
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


class UploadClient(Protocol):
    async def upload(self, record: ConversationRecord) -> dict | None:
        """Upload one record. Return the server's response body as a dict
        (at minimum ``{"capture_id": ...}``), or ``None`` when no response
        body is available (e.g. the no-op client). Raise on failure.

        The response's ``title`` / ``summary`` are server placeholders and
        are NOT applied here — the agent polls ``GET /api/captures/{id}``
        for the real values via ``CapturePoller``.
        """
        ...


class NoopUploadClient:
    async def upload(self, record: ConversationRecord) -> dict | None:
        log.info("[upload] (noop) record id=%s", record.id)
        return None


class AuthenticatedUploadClient:
    """Upload captures to the server via multipart POST.

    Raises on any failure — the caller (UploadRetryManager) classifies the
    exception, updates record state, and decides whether to retry. Returns
    the parsed JSON response body on success.
    """

    def __init__(self, auth_client, captures_dir: Path) -> None:
        from .auth.client import AuthenticatedClient
        self._client: AuthenticatedClient = auth_client
        self._captures_dir = captures_dir

    async def upload(self, record: ConversationRecord) -> dict | None:
        audio_path = self._captures_dir / record.id / AUDIO_FILENAME
        if not audio_path.exists():
            raise FileNotFoundError(f"audio file not found: {audio_path}")

        record_json = json.dumps(
            serialize_record_for_upload(record), ensure_ascii=False
        )
        with open(audio_path, "rb") as audio_file:
            resp = await self._client.post(
                "/api/captures/upload",
                data={"record": record_json},
                files={"audio": (AUDIO_FILENAME, audio_file, "audio/ogg")},
                headers={"X-Agent-Version": __version__},
                timeout=httpx.Timeout(60.0),
            )
        resp.raise_for_status()
        body = parse_json_body(resp)
        capture_id = body.get("capture_id") if body else None
        log.info("[upload] success id=%s server_id=%s", record.id, capture_id or "?")
        return body
