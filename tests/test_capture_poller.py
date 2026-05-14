"""Tests for sayzo_agent.capture_poller.CapturePoller.

The poller fires GET /api/captures/{id} on a sparse schedule after each
upload success. When the server reports a post-transcription status, the
poller caches title/summary into local record.json. We exercise the
schedule, the status gating, and the no-auth-client no-op fallback.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import httpx
import pytest

from sayzo_agent.auth.exceptions import AuthenticationRequired
from sayzo_agent.capture_poller import CapturePoller
from sayzo_agent.models import ConversationRecord
from sayzo_agent.retry import empty_upload_state
from sayzo_agent.sink import serialize_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAuthClient:
    """Mimics AuthenticatedClient.get(path, ...). Each call pops the next
    scripted response off `responses`. A response is either a dict body
    (returned as status=200 JSON) or an Exception to raise."""

    def __init__(self) -> None:
        self.responses: list = []
        self.calls: list[str] = []

    def queue(self, body_or_exc) -> None:
        self.responses.append(body_or_exc)

    async def get(self, path: str, **kwargs) -> httpx.Response:
        self.calls.append(path)
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        # Construct a real httpx.Response so the poller's .raise_for_status()
        # and .json() paths exercise correctly.
        body = json.dumps(item).encode()
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )


def _write_capture(captures_dir: Path, rec_id: str) -> Path:
    rec_dir = captures_dir / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec = ConversationRecord(
        id=rec_id,
        started_at=datetime(2026, 5, 14, 14, 32, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 14, 14, 47, 18, tzinfo=timezone.utc),
        title="Conversation · 2026-05-14 14:32",  # local placeholder
        summary="",
        metadata={"close_reason": "joint_silence", "upload": empty_upload_state()},
    )
    (rec_dir / "record.json").write_text(
        json.dumps(serialize_record(rec)), encoding="utf-8"
    )
    return rec_dir


@pytest.fixture
def env(tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-poller")
    auth = FakeAuthClient()
    # Tiny real delays so the test runs in milliseconds without interfering
    # with pytest-asyncio's event-loop machinery (patching asyncio.sleep
    # globally broke status propagation between ticks).
    poller = CapturePoller(
        auth_client=auth,
        captures_dir=captures_dir,
        executor=executor,
        schedule=(0.001, 0.001, 0.001),
    )
    try:
        yield SimpleNamespace(
            captures_dir=captures_dir,
            executor=executor,
            auth=auth,
            poller=poller,
        )
    finally:
        executor.shutdown(wait=True)


def _read_record(rec_dir: Path) -> dict:
    return json.loads((rec_dir / "record.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_poller_caches_title_on_transcribed(env):
    rec_dir = _write_capture(env.captures_dir, "rec_t1")
    env.auth.queue({"status": "queued"})
    env.auth.queue({
        "status": "transcribed",
        "title": "Standup with backend team",
        "summary": "Discussed indexer rollout.",
    })
    env.auth.queue({"status": "analyzed"})  # terminal — stops polling

    await env.poller.poll(rec_dir, "srv_t1")
    data = _read_record(rec_dir)
    assert data["title"] == "Standup with backend team"
    assert data["summary"] == "Discussed indexer rollout."


async def test_poller_stops_on_terminal_status_without_overwrite(env):
    """If the server reaches a terminal status (rejected) without ever
    going past transcribed with non-empty title, the local placeholder
    stays untouched."""
    rec_dir = _write_capture(env.captures_dir, "rec_rejected")
    env.auth.queue({"status": "queued"})
    env.auth.queue({"status": "rejected"})
    # Extra queued responses must NOT be consumed.
    env.auth.queue({"status": "analyzed", "title": "should not appear"})

    await env.poller.poll(rec_dir, "srv_rejected")
    data = _read_record(rec_dir)
    assert data["title"] == "Conversation · 2026-05-14 14:32"
    # The rejected response was the second call; the third was never made.
    assert len(env.auth.calls) == 2


async def test_poller_no_auth_client_is_noop(tmp_path):
    """When the agent runs against NoopUploadClient (signed-out), the
    poller is constructed with auth_client=None and must do nothing."""
    captures_dir = tmp_path / "captures_noop"
    captures_dir.mkdir()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        rec_dir = _write_capture(captures_dir, "rec_noop")
        poller = CapturePoller(
            auth_client=None,
            captures_dir=captures_dir,
            executor=executor,
            schedule=(0.0,),
        )
        await poller.poll(rec_dir, "srv_noop")
        # Placeholder unchanged.
        data = _read_record(rec_dir)
        assert data["title"] == "Conversation · 2026-05-14 14:32"
    finally:
        executor.shutdown(wait=True)


async def test_poller_auth_required_aborts_quietly(env):
    """AuthenticationRequired from the auth client (token expired) ends
    polling without raising. Local placeholder stays."""
    rec_dir = _write_capture(env.captures_dir, "rec_auth")
    env.auth.queue(AuthenticationRequired("token expired"))
    env.auth.queue({"status": "analyzed", "title": "Never seen"})

    # Should not raise.
    await env.poller.poll(rec_dir, "srv_auth")
    data = _read_record(rec_dir)
    assert data["title"] == "Conversation · 2026-05-14 14:32"
    # Only the first call was made.
    assert len(env.auth.calls) == 1


async def test_poller_writes_only_when_value_changes(env):
    """If the server's title equals the existing local title, no rewrite
    happens (avoid no-op write churn)."""
    rec_dir = _write_capture(env.captures_dir, "rec_same")
    env.auth.queue({
        "status": "transcribed",
        "title": "Conversation · 2026-05-14 14:32",  # same as placeholder
        "summary": "",  # same as placeholder
    })
    env.auth.queue({"status": "analyzed"})

    mtime_before = (rec_dir / "record.json").stat().st_mtime_ns
    await env.poller.poll(rec_dir, "srv_same")
    mtime_after = (rec_dir / "record.json").stat().st_mtime_ns
    # The file may not be rewritten when nothing changed.
    assert mtime_after == mtime_before
