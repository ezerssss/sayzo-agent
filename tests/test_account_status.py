"""Tests for sayzo_agent.account.status.

Mirrors tests/test_daily_drill_api.py — uses ``httpx.MockTransport`` so
we exercise the real ``AuthenticatedClient`` code path while controlling
responses. Verifies the typed dispatch + the server-state mapping (the
backend uses ``"active"``; the agent maps to ``"ok"`` to match the
daily-drill convention).
"""
from __future__ import annotations

import random
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sayzo_agent.account.status import (
    AccountStatusResponse,
    fetch_account_status,
)
from sayzo_agent.auth.client import AuthenticatedClient
from sayzo_agent.auth.exceptions import AuthenticationRequired


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
# 200 OK — happy path with the documented response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_200_active_maps_to_ok(patch_async_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/me"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "user_id": "usr_abc",
                "email": "user@example.com",
                "onboarding_complete": True,
                "onboarding_url": "https://sayzo.app/onboarding",
                "account_state": "active",
                "issued_at": "2026-05-04T12:00:00Z",
            },
        )

    patch_async_client(handler)
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "ok"
    assert resp.onboarding_complete is True
    assert resp.user_id == "usr_abc"
    assert resp.email == "user@example.com"
    assert resp.onboarding_url == "https://sayzo.app/onboarding"
    assert resp.is_allowed is True
    assert resp.is_persistable is True


@pytest.mark.asyncio
async def test_200_onboarding_required(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={
                "user_id": "usr_x",
                "email": "user@example.com",
                "onboarding_complete": False,
                "onboarding_url": "https://sayzo.app/onboarding",
                "account_state": "onboarding_required",
            },
        )
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "onboarding_required"
    assert resp.is_allowed is False
    assert resp.is_persistable is True


@pytest.mark.asyncio
async def test_200_suspended(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={"user_id": "u", "account_state": "suspended"},
        )
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "suspended"


@pytest.mark.asyncio
async def test_200_deleted(patch_async_client) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={"user_id": "u", "account_state": "deleted"},
        )
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "deleted"


@pytest.mark.asyncio
async def test_200_missing_account_state_falls_back_to_onboarding_complete(
    patch_async_client,
) -> None:
    """An early backend version that ships only ``onboarding_complete``
    (without the ``account_state`` enum) should still gate correctly."""
    patch_async_client(
        lambda req: httpx.Response(200, json={"onboarding_complete": False})
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "onboarding_required"

    patch_async_client(
        lambda req: httpx.Response(200, json={"onboarding_complete": True})
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "ok"


@pytest.mark.asyncio
async def test_200_unknown_account_state_returns_unknown_error(
    patch_async_client,
) -> None:
    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={"account_state": "definitely_not_a_real_state"},
        )
    )
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "unknown_error"


@pytest.mark.asyncio
async def test_200_non_json_body_returns_unknown_error(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(200, content=b"<html>oops</html>"))
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "unknown_error"


# ---------------------------------------------------------------------------
# Status-code branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_returns_auth_required(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(401))
    resp = await fetch_account_status(_make_real_client())
    # AuthenticatedClient retries once on 401; both responses are 401 →
    # raises AuthenticationRequired which we map to auth_required.
    assert resp.status == "auth_required"
    assert resp.is_persistable is False


@pytest.mark.asyncio
async def test_token_store_raises_auth_required(patch_async_client) -> None:
    store = MagicMock()
    store.get_valid_token = AsyncMock(
        side_effect=AuthenticationRequired("no token")
    )
    client = AuthenticatedClient("https://sayzo.app", store)
    patch_async_client(lambda req: httpx.Response(500))
    resp = await fetch_account_status(client)
    assert resp.status == "auth_required"


@pytest.mark.asyncio
async def test_404_maps_to_deleted(patch_async_client) -> None:
    """Backend may signal account-deleted via 404 instead of a 200 with
    ``account_state="deleted"``. Both paths must converge."""
    patch_async_client(lambda req: httpx.Response(404))
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "deleted"


@pytest.mark.asyncio
async def test_410_maps_to_deleted(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(410))
    resp = await fetch_account_status(_make_real_client())
    assert resp.status == "deleted"


@pytest.mark.asyncio
async def test_other_4xx_returns_unknown_error(patch_async_client) -> None:
    patch_async_client(lambda req: httpx.Response(418))
    resp = await fetch_account_status(_make_real_client())
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
    resp = await fetch_account_status(
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
        return httpx.Response(200, json={"account_state": "active"})

    patch_async_client(handler)
    resp = await fetch_account_status(
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
    resp = await fetch_account_status(
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
    resp = await fetch_account_status(
        _make_real_client(), max_retries=2, base_backoff_secs=0.0
    )
    assert resp.status == "transient_error"


# ---------------------------------------------------------------------------
# Backoff growth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_grows_exponentially(patch_async_client, monkeypatch) -> None:
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    monkeypatch.setattr("sayzo_agent.account.status.asyncio.sleep", fake_sleep)
    patch_async_client(lambda req: httpx.Response(503))

    await fetch_account_status(
        _make_real_client(),
        max_retries=3,
        base_backoff_secs=2.0,
        rng=random.Random(0),
    )
    assert len(delays) == 2
    assert 1.8 <= delays[0] <= 2.2
    assert 3.6 <= delays[1] <= 4.4


# ---------------------------------------------------------------------------
# refresh_and_cache helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_and_cache_writes_on_persistable(
    patch_async_client, tmp_path
) -> None:
    from sayzo_agent.account import refresh_and_cache, read_cache
    from sayzo_agent.config import Config

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    patch_async_client(
        lambda req: httpx.Response(
            200,
            json={
                "user_id": "u",
                "email": "e@x.com",
                "onboarding_complete": True,
                "onboarding_url": "https://sayzo.app/onboarding",
                "account_state": "active",
            },
        )
    )

    response = await refresh_and_cache(_make_real_client(), cfg)
    assert response.status == "ok"
    cached = read_cache(cfg)
    assert cached is not None
    assert cached.account_state == "ok"
    assert cached.email == "e@x.com"


@pytest.mark.asyncio
async def test_refresh_and_cache_does_not_overwrite_on_transient(
    patch_async_client, tmp_path
) -> None:
    """A flaky network must NOT downgrade a positive cache. Write a good
    cache first, then have the next fetch fail — read should still show
    the original."""
    from sayzo_agent.account import (
        CachedAccountStatus,
        refresh_and_cache,
        read_cache,
        write_cache,
    )
    from sayzo_agent.config import Config

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    write_cache(
        cfg,
        CachedAccountStatus(
            account_state="ok",
            onboarding_complete=True,
            onboarding_url=None,
            email=None,
            user_id="usr_pre",
            fetched_at="2026-05-04T12:00:00+00:00",
        ),
    )

    patch_async_client(lambda req: httpx.Response(503))
    response = await refresh_and_cache(
        _make_real_client(), cfg, max_retries=2, base_backoff_secs=0.0
    )
    assert response.status == "transient_error"
    # Cache should still hold the original positive state.
    cached = read_cache(cfg)
    assert cached is not None
    assert cached.account_state == "ok"
    assert cached.user_id == "usr_pre"
