"""Tests for the /api/sessions/today client.

Uses ``httpx.MockTransport`` so we exercise the real ``httpx.AsyncClient``
code path inside ``AuthenticatedClient`` while controlling responses.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sayzo_agent.auth.client import AuthenticatedClient
from sayzo_agent.auth.exceptions import AuthenticationRequired
from sayzo_agent.daily_drill.api import (
    TodaySessionResponse,
    fetch_today_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    raises_auth_required: bool = False,
) -> AuthenticatedClient:
    """Build an AuthenticatedClient whose underlying httpx.AsyncClient
    routes through the supplied handler instead of the network."""
    store = MagicMock()
    if raises_auth_required:
        store.get_valid_token = AsyncMock(side_effect=AuthenticationRequired("no token"))
    else:
        store.get_valid_token = AsyncMock(return_value="dummy-token")

    client = AuthenticatedClient("https://sayzo.app", store)
    transport = httpx.MockTransport(handler)

    # Replace the AsyncClient construction inside .request with one that
    # uses our transport. AuthenticatedClient builds a new client per call;
    # patch it to inject the transport.
    original_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(original_async_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    # Patch is scoped per-call below via a module-level monkeypatch fixture.
    client._patched_async_client = _PatchedAsyncClient  # type: ignore[attr-defined]
    return client


@pytest.fixture
def patch_async_client(monkeypatch):
    """Inject httpx.MockTransport into AuthenticatedClient's per-call
    AsyncClient construction so tests don't open real sockets."""

    transports: list[httpx.MockTransport] = []

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        transports.append(httpx.MockTransport(handler))

    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            t = transports[-1] if transports else None
            super().__init__(*args, transport=t, **kwargs)

    monkeypatch.setattr("sayzo_agent.auth.client.httpx.AsyncClient", _Patched)
    return install


def _make_real_client(token: str = "tok") -> AuthenticatedClient:
    store = MagicMock()
    store.get_valid_token = AsyncMock(return_value=token)
    return AuthenticatedClient("https://sayzo.app", store)


# ---------------------------------------------------------------------------
# 200 OK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_200_returns_ok_with_fields_extracted(patch_async_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sessions/today"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "sessionId": "sess_abc",
                "deepLinkUrl": "https://sayzo.app/drills/sess_abc",
                "isReplay": False,
                "scenarioTitle": "Monday standup",
                "question": "Give a standup.",
            },
        )

    patch_async_client(handler)
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "ok"
    assert resp.session_id == "sess_abc"
    assert resp.deep_link_url == "https://sayzo.app/drills/sess_abc"
    assert resp.is_replay is False
    assert resp.scenario_title == "Monday standup"
    assert resp.question == "Give a standup."
    assert resp.fireable is True


@pytest.mark.asyncio
async def test_200_with_replay_flag(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={
                "sessionId": "s",
                "deepLinkUrl": "u",
                "isReplay": True,
                "scenarioTitle": None,
                "question": "redo",
            },
        )
    )
    resp = await fetch_today_session(_make_real_client())
    assert resp.is_replay is True


@pytest.mark.asyncio
async def test_200_with_non_json_body_returns_unknown_error(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(200, content=b"<html>oops</html>"))
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "unknown_error"


# ---------------------------------------------------------------------------
# 402 (regression guard — should NOT reach the dead over_credit branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_402_falls_through_to_unknown_error(patch_async_client) -> None:
    """v3.6.5: 402 branch deleted (platform never returns 402 on /today).

    If a future regression causes the platform to return 402 here, the
    Other-4xx fallthrough maps it to unknown_error — the scheduler then
    treats that as transient and retries next tick, instead of silently
    locking the day under the old over_credit semantics.
    """
    patch_async_client(lambda req: httpx.Response(402, json={"code": "CREDIT_LIMIT"}))
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "unknown_error"
    assert resp.fireable is False


# ---------------------------------------------------------------------------
# 409 still-processing / retry-required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_409_still_processing(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            409,
            json={
                "code": "DRILL_STILL_PROCESSING",
                "sessionId": "sess_existing",
                "deepLinkUrl": "https://sayzo.app/drills/sess_existing",
            },
        )
    )
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "still_processing"
    assert resp.session_id == "sess_existing"
    assert resp.deep_link_url == "https://sayzo.app/drills/sess_existing"
    assert resp.fireable is True


@pytest.mark.asyncio
async def test_409_retry_required(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            409,
            json={
                "code": "DRILL_RETRY_REQUIRED",
                "deepLinkUrl": "https://sayzo.app/drills/x",
            },
        )
    )
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "retry_required"
    assert resp.fireable is True


# ---------------------------------------------------------------------------
# 401 / auth required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_returns_auth_required(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(401))
    resp = await fetch_today_session(_make_real_client())
    # AuthenticatedClient retries once on 401; both attempts return 401 →
    # raises AuthenticationRequired which we map to auth_required.
    assert resp.status == "auth_required"


@pytest.mark.asyncio
async def test_token_store_raises_auth_required(patch_async_client) -> None:
    store = MagicMock()
    store.get_valid_token = AsyncMock(side_effect=AuthenticationRequired("no token"))
    client = AuthenticatedClient("https://sayzo.app", store)
    # No handler needed — request should not reach transport.
    patch_async_client(lambda req: httpx.Response(500))
    resp = await fetch_today_session(client)
    assert resp.status == "auth_required"


# ---------------------------------------------------------------------------
# Other 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_4xx_returns_unknown_error(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(418))
    resp = await fetch_today_session(_make_real_client())
    assert resp.status == "unknown_error"


# ---------------------------------------------------------------------------
# 5xx with retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_500_retries_then_returns_transient_error(patch_async_client) -> None:
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503)

    patch_async_client(handler)
    resp = await fetch_today_session(
        _make_real_client(),
        max_retries=3,
        base_backoff_secs=0.0,  # speed up
    )
    assert resp.status == "transient_error"
    assert call_count == 3


@pytest.mark.asyncio
async def test_500_then_200_returns_ok(patch_async_client) -> None:
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "sessionId": "sess",
                "deepLinkUrl": "u",
                "isReplay": False,
                "scenarioTitle": "t",
                "question": "q",
            },
        )

    patch_async_client(handler)
    resp = await fetch_today_session(
        _make_real_client(),
        max_retries=3,
        base_backoff_secs=0.0,
    )
    assert resp.status == "ok"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_retries_then_gives_up(patch_async_client) -> None:
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("boom")

    patch_async_client(handler)
    resp = await fetch_today_session(
        _make_real_client(),
        max_retries=2,
        base_backoff_secs=0.0,
    )
    assert resp.status == "transient_error"
    assert call_count == 2


@pytest.mark.asyncio
async def test_timeout_retries_then_gives_up(patch_async_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    patch_async_client(handler)
    resp = await fetch_today_session(
        _make_real_client(), max_retries=2, base_backoff_secs=0.0
    )
    assert resp.status == "transient_error"


# ---------------------------------------------------------------------------
# Extended read timeout (v3.6.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_today_call_passes_extended_timeout(monkeypatch) -> None:
    """v3.6.5: /sessions/today must be called with an extended read
    timeout because the platform runs synchronous LLM generation inside
    the request. httpx's 5s default would silently fail on slow LLM
    days and the agent would never fire."""
    from sayzo_agent.daily_drill import api as api_mod

    captured: dict = {}

    async def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["timeout"] = kwargs.get("timeout")
        return httpx.Response(
            200,
            json={
                "sessionId": "s",
                "deepLinkUrl": "u",
                "isReplay": False,
                "scenarioTitle": "t",
                "question": "q",
            },
        )

    monkeypatch.setattr(AuthenticatedClient, "request", fake_request)
    resp = await fetch_today_session(_make_real_client())

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/sessions/today"
    # The exact Timeout object is passed through — read deadline 45s.
    assert captured["timeout"] is api_mod._TODAY_TIMEOUT
    assert captured["timeout"].read == 45.0
    assert resp.status == "ok"


# ---------------------------------------------------------------------------
# Backoff growth (we check sleep delays via patched asyncio.sleep)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_grows_exponentially(patch_async_client, monkeypatch) -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    monkeypatch.setattr("sayzo_agent.daily_drill.api.asyncio.sleep", fake_sleep)
    patch_async_client(lambda req: httpx.Response(503))

    await fetch_today_session(
        _make_real_client(),
        max_retries=3,
        base_backoff_secs=2.0,
        rng=random.Random(0),
    )
    # Two backoff sleeps between 3 attempts; jitter ±10% means rough bounds.
    assert len(delays) == 2
    assert 1.8 <= delays[0] <= 2.2
    assert 3.6 <= delays[1] <= 4.4
