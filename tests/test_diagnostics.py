"""Tests for sayzo_agent.diagnostics + the Layer-1 /api/me header plumbing.

The uploader tests reuse the ``httpx.MockTransport`` pattern from
``test_account_status`` so the real ``AuthenticatedClient.post`` path runs
(auth header injection, multipart encoding) against controlled responses.
"""
from __future__ import annotations

import gzip
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sayzo_agent import __version__
from sayzo_agent.auth.client import AuthenticatedClient
from sayzo_agent.config import Config
from sayzo_agent import diagnostics
from sayzo_agent.account.status import _parse_200, fetch_account_status


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **over) -> Config:
    return Config(data_dir=tmp_path, **over)


def test_diagnostics_headers_on_returns_three(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    h = diagnostics.diagnostics_headers(cfg)
    assert set(h) == {"X-Agent-Version", "X-Agent-Platform", "X-Agent-Install-Id"}
    assert h["X-Agent-Version"] == __version__


def test_diagnostics_headers_off_is_empty_and_writes_no_install_id(tmp_path) -> None:
    cfg = _cfg(tmp_path, share_diagnostics=False)
    assert diagnostics.diagnostics_headers(cfg) == {}
    # Opting out must NOT create a persistent identifier.
    assert not (tmp_path / diagnostics.INSTALL_ID_FILENAME).exists()


def test_build_platform_string_shape() -> None:
    s = diagnostics.build_platform_string()
    # "<sys.platform>;<platform.platform()>;py<ver>"
    assert s.count(";") == 2
    assert ";py" in s


def test_install_id_is_stable_and_persisted(tmp_path) -> None:
    first = diagnostics.get_or_create_install_id(tmp_path)
    second = diagnostics.get_or_create_install_id(tmp_path)
    assert first == second
    assert (tmp_path / diagnostics.INSTALL_ID_FILENAME).read_text().strip() == first


def test_build_meta_carries_contract_fields(tmp_path) -> None:
    meta = diagnostics.build_meta(_cfg(tmp_path), "crash")
    assert set(meta) >= {"version", "platform", "install_id", "reason", "captured_at"}
    assert meta["reason"] == "crash"
    assert meta["version"] == __version__


def test_collect_log_parts_gzips_present_skips_missing(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_bytes(b"hello log\n")
    # Leave agent.log.1 missing; provide .2 to prove gaps are tolerated.
    (logs / "agent.log.2").write_bytes(b"older\n")
    parts = diagnostics._collect_log_parts(logs)
    names = [p[1][0] for p in parts]
    assert names == ["agent.log.gz", "agent.log.2.gz"]
    # Round-trips back to the original bytes.
    first_blob = parts[0][1][1]
    assert gzip.decompress(first_blob) == b"hello log\n"
    assert all(p[1][2] == "application/gzip" for p in parts)


def test_collect_log_parts_empty_when_no_logs(tmp_path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    assert diagnostics._collect_log_parts(logs) == []


def test_crash_sentinel_roundtrip(tmp_path) -> None:
    assert not diagnostics.crash_sentinel_path(tmp_path).exists()
    diagnostics.write_crash_sentinel(tmp_path, "boom")
    p = diagnostics.crash_sentinel_path(tmp_path)
    assert p.exists()
    assert p.read_text() == "boom"


def test_parse_200_surfaces_collect_logs() -> None:
    base = {
        "user_id": "u1",
        "email": "a@b.c",
        "onboarding_complete": True,
        "account_state": "active",
    }
    assert _parse_200(httpx.Response(200, json={**base, "collect_logs": True})).collect_logs is True
    # Absent flag defaults False (never None on a 200).
    assert _parse_200(httpx.Response(200, json=base)).collect_logs is False


# ---------------------------------------------------------------------------
# Uploader — real AuthenticatedClient.post path via MockTransport
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_async_client(monkeypatch):
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


def _client(token: str = "tok") -> AuthenticatedClient:
    store = MagicMock()
    store.get_valid_token = AsyncMock(return_value=token)
    return AuthenticatedClient("https://sayzo.app", store)


def _seed_log(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.logs_dir / "agent.log").write_bytes(b"some diagnostic line\n")
    return cfg


async def test_upload_success_posts_multipart(tmp_path, patch_async_client) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["ver"] = request.headers.get("x-agent-version")
        seen["auth"] = request.headers.get("authorization")
        body = request.content
        seen["has_meta"] = b'name="meta"' in body
        seen["has_reason"] = b"on_demand" in body
        return httpx.Response(200, json={"ok": True})

    patch_async_client(handler)
    cfg = _seed_log(tmp_path)
    body = await diagnostics.DiagnosticsUploader(_client(), cfg).upload("on_demand")
    assert body == {"ok": True}
    assert seen["path"] == "/api/diagnostics/upload"
    assert seen["method"] == "POST"
    assert seen["ver"] == __version__
    assert seen["auth"] == "Bearer tok"
    assert seen["has_meta"] and seen["has_reason"]


async def test_try_upload_swallows_http_error(tmp_path, patch_async_client) -> None:
    patch_async_client(lambda request: httpx.Response(500))
    cfg = _seed_log(tmp_path)
    uploader = diagnostics.DiagnosticsUploader(_client(), cfg)
    # raw upload raises…
    with pytest.raises(httpx.HTTPStatusError):
        await uploader.upload("crash")
    # …but the fire-and-forget wrapper just returns False.
    assert await uploader.try_upload("crash") is False


async def test_upload_noop_when_no_logs(tmp_path, patch_async_client) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"ok": True})

    patch_async_client(handler)
    cfg = Config(data_dir=tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)  # exists but empty
    result = await diagnostics.DiagnosticsUploader(_client(), cfg).upload("on_demand")
    assert result is None
    assert called["n"] == 0  # never hit the network


async def test_fetch_account_status_sends_extra_headers(tmp_path, patch_async_client) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ver"] = request.headers.get("x-agent-version")
        seen["plat"] = request.headers.get("x-agent-platform")
        return httpx.Response(200, json={"account_state": "active", "onboarding_complete": True})

    patch_async_client(handler)
    await fetch_account_status(
        _client(),
        extra_headers={"X-Agent-Version": "9.9.9", "X-Agent-Platform": "win32;X;py3"},
    )
    assert seen["ver"] == "9.9.9"
    assert seen["plat"] == "win32;X;py3"
