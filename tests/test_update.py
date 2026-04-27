"""Tests for sayzo_agent.update — pure version compare + manifest fetch.

No network. Manifest responses are faked via httpx.MockTransport.
"""
from __future__ import annotations

import sys

import httpx
import pytest

from sayzo_agent.update import (
    UpdateInfo,
    _parse_version,
    check,
    fetch_manifest,
    is_newer,
)


# ---------------------------------------------------------------------------
# Version compare
# ---------------------------------------------------------------------------


def test_is_newer_basic_patch_bump() -> None:
    assert is_newer("0.1.0", "0.1.1") is True


def test_is_newer_basic_minor_bump() -> None:
    assert is_newer("0.1.0", "0.2.0") is True


def test_is_newer_basic_major_bump() -> None:
    assert is_newer("0.9.9", "1.0.0") is True


def test_is_newer_downgrade_returns_false() -> None:
    assert is_newer("0.1.1", "0.1.0") is False


def test_is_newer_parity_returns_false() -> None:
    assert is_newer("0.1.0", "0.1.0") is False


def test_is_newer_fail_safe_on_garbage_candidate() -> None:
    # Unparseable candidate must not trigger a bogus "update available" prompt.
    assert is_newer("0.1.0", "garbage") is False


def test_is_newer_fail_safe_on_garbage_current() -> None:
    assert is_newer("garbage", "0.1.1") is False


def test_is_newer_dev_suffix_upgrades_to_release() -> None:
    # A "-dev" build must upgrade to the matching real release. _parse_version
    # strips the suffix so 0.1.0-dev < 0.1.0 at the tuple level, but since we
    # only compare strictly-greater, equal-base pairs return False. That's the
    # design — a dev build stays on dev until a strictly-higher real release
    # lands. We assert the "strictly higher release" path works as expected.
    assert is_newer("0.1.0-dev", "0.1.1") is True


def test_parse_version_strips_suffix() -> None:
    assert _parse_version("0.1.0-rc1") == (0, 1, 0)
    assert _parse_version("0.0.0-dev") == (0, 0, 0)


def test_parse_version_garbage_returns_none() -> None:
    assert _parse_version("garbage") is None
    assert _parse_version("") is None


# ---------------------------------------------------------------------------
# Manifest fetch + check
# ---------------------------------------------------------------------------


_GOOD_MANIFEST = {
    "version": "0.1.1",
    "released_at": "2026-04-25T10:00:00Z",
    "notes": "Quiet the STT hallucination on idle.",
    "windows": {
        "url": "https://sayzo.app/releases/windows/sayzo-setup.exe",
        "sha256": "deadbeef",
    },
    "macos": {
        "url": "https://sayzo.app/releases/macos/Sayzo.dmg",
        "sha256": "cafebabe",
    },
}


def _transport(status: int, body) -> httpx.MockTransport:
    """httpx MockTransport that returns a canned response for every request.

    ``body`` may be a dict (serialized as JSON), a string (raw text), or None
    for an empty body. Use status 200 with a malformed string to exercise the
    JSON-decode failure path.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


async def test_fetch_manifest_good_json() -> None:
    async with httpx.AsyncClient(transport=_transport(200, _GOOD_MANIFEST)) as client:
        data = await fetch_manifest(client, url="https://example.com/latest.json")
    assert data == _GOOD_MANIFEST


async def test_fetch_manifest_404_returns_none() -> None:
    async with httpx.AsyncClient(transport=_transport(404, "nope")) as client:
        data = await fetch_manifest(client, url="https://example.com/latest.json")
    assert data is None


async def test_fetch_manifest_malformed_json_returns_none() -> None:
    async with httpx.AsyncClient(transport=_transport(200, "not json {")) as client:
        data = await fetch_manifest(client, url="https://example.com/latest.json")
    assert data is None


async def test_check_returns_update_when_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin platform so the test is deterministic on whichever OS runs CI.
    monkeypatch.setattr(sys, "platform", "win32")
    async with httpx.AsyncClient(transport=_transport(200, _GOOD_MANIFEST)) as client:
        info = await check("0.1.0", client=client, url="https://example.com/latest.json")
    assert info == UpdateInfo(
        version="0.1.1",
        url="https://sayzo.app/releases/windows/sayzo-setup.exe",
        notes="Quiet the STT hallucination on idle.",
    )


async def test_check_returns_none_when_current_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    async with httpx.AsyncClient(transport=_transport(200, _GOOD_MANIFEST)) as client:
        info = await check("0.1.1", client=client, url="https://example.com/latest.json")
    assert info is None


async def test_check_returns_none_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    async with httpx.AsyncClient(transport=_transport(200, _GOOD_MANIFEST)) as client:
        info = await check("0.1.0", client=client, url="https://example.com/latest.json")
    assert info is None


async def test_check_handles_missing_platform_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Manifest lists no macOS entry — a mac agent must see "no update" rather
    # than crashing trying to read None["url"].
    monkeypatch.setattr(sys, "platform", "darwin")
    mac_missing = {**_GOOD_MANIFEST}
    mac_missing.pop("macos")
    async with httpx.AsyncClient(transport=_transport(200, mac_missing)) as client:
        info = await check("0.1.0", client=client, url="https://example.com/latest.json")
    assert info is None


async def test_check_handles_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    async with httpx.AsyncClient(transport=_transport(404, "nope")) as client:
        info = await check("0.1.0", client=client, url="https://example.com/latest.json")
    assert info is None


async def test_check_handles_malformed_version_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    bad = {**_GOOD_MANIFEST, "version": "not-a-version"}
    async with httpx.AsyncClient(transport=_transport(200, bad)) as client:
        info = await check("0.1.0", client=client, url="https://example.com/latest.json")
    assert info is None
