"""Tests for sayzo_agent.update_stage — atomic staged download + hash verify.

No network: payloads are served via ``httpx.MockTransport``. No disk side
effects outside ``tmp_path`` (pytest auto-cleans).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

from sayzo_agent.update import UpdateInfo
from sayzo_agent.update_stage import (
    MANIFEST_NAME,
    StagedUpdate,
    clear_staged,
    download_and_stage,
    read_staged,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _bytes_transport(body: bytes, *, status: int = 200) -> httpx.MockTransport:
    """MockTransport that returns ``body`` verbatim, with a Content-Length
    header so the progress logger has something to chew on."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body,
            headers={"content-length": str(len(body))},
        )
    return httpx.MockTransport(handler)


def _failing_transport() -> httpx.MockTransport:
    """MockTransport that raises a transport-level error to simulate a
    mid-download network failure (DNS, TCP reset, etc.)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")
    return httpx.MockTransport(handler)


def _info(body: bytes, version: str = "0.1.1") -> UpdateInfo:
    return UpdateInfo(
        version=version,
        url="https://example.com/sayzo-setup.exe",
        notes="Quiet the STT hallucination on idle.",
        sha256=_sha256(body),
    )


# ---------------------------------------------------------------------------
# download_and_stage
# ---------------------------------------------------------------------------


async def test_download_and_stage_writes_payload_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    body = b"PRETEND-EXE-BYTES" * 100
    info = _info(body)

    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is not None
    assert staged.version == "0.1.1"
    assert staged.platform == "windows"
    assert staged.sha256 == info.sha256
    assert staged.payload_path == tmp_path / "staged_update" / "payload.exe"
    assert staged.payload_path.read_bytes() == body

    manifest_path = tmp_path / "staged_update" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == "0.1.1"
    assert manifest["platform"] == "windows"
    assert manifest["sha256"] == info.sha256
    assert manifest["notes"] == info.notes
    assert manifest["ready_at"]  # non-empty ISO timestamp

    # .partial file must not survive a successful download.
    assert not (tmp_path / "staged_update" / "payload.exe.partial").exists()


async def test_download_and_stage_picks_dmg_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    body = b"PRETEND-DMG-BYTES"
    info = _info(body)

    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is not None
    assert staged.platform == "macos"
    assert staged.payload_path.name == "payload.dmg"


async def test_download_and_stage_unsupported_platform_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    body = b"junk"
    info = _info(body)

    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is None
    # No files should have been written outside the existing tmp_path.
    assert not (tmp_path / "staged_update").exists()


async def test_download_and_stage_hash_mismatch_discards_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    body = b"WHAT-THE-SERVER-RETURNS"
    info = UpdateInfo(
        version="0.1.1",
        url="https://example.com/sayzo-setup.exe",
        notes="",
        # Hash of something completely different.
        sha256=_sha256(b"WHAT-THE-MANIFEST-PROMISED"),
    )

    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is None
    # No payload, no manifest, no leftover partial.
    staged_dir = tmp_path / "staged_update"
    assert not (staged_dir / "payload.exe").exists()
    assert not (staged_dir / MANIFEST_NAME).exists()
    assert not (staged_dir / "payload.exe.partial").exists()


async def test_download_and_stage_network_error_discards_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    info = _info(b"never-arrives")

    async with httpx.AsyncClient(transport=_failing_transport()) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is None
    staged_dir = tmp_path / "staged_update"
    # The dir was created during setup, but no files should remain.
    if staged_dir.exists():
        assert list(staged_dir.iterdir()) == []


async def test_download_and_stage_clears_stale_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A crashed prior run left a partial behind. The new download must not
    # append to those bytes — verify by staging successfully and checking the
    # final payload matches the new body exactly.
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir(parents=True)
    (staged_dir / "payload.exe.partial").write_bytes(b"STALE-BYTES-FROM-CRASH")

    body = b"FRESH-BYTES"
    info = _info(body)
    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        staged = await download_and_stage(info, tmp_path, client=client)

    assert staged is not None
    assert staged.payload_path.read_bytes() == body


# ---------------------------------------------------------------------------
# read_staged
# ---------------------------------------------------------------------------


async def test_read_staged_returns_none_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert read_staged(tmp_path) is None


async def test_read_staged_returns_stage_after_successful_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    body = b"PRETEND-EXE-BYTES"
    info = _info(body, version="0.2.0")
    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        await download_and_stage(info, tmp_path, client=client)

    staged = read_staged(tmp_path)
    assert staged is not None
    assert staged.version == "0.2.0"
    assert staged.platform == "windows"
    assert staged.payload_path == tmp_path / "staged_update" / "payload.exe"


def test_read_staged_returns_none_when_manifest_present_payload_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir()
    (staged_dir / MANIFEST_NAME).write_text(
        json.dumps({
            "version": "0.1.1",
            "platform": "windows",
            "sha256": "deadbeef",
            "notes": "",
            "ready_at": "2026-05-12T00:00:00Z",
        }),
        encoding="utf-8",
    )
    # Payload not written — inconsistent state.
    assert read_staged(tmp_path) is None


def test_read_staged_returns_none_when_payload_present_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir()
    (staged_dir / "payload.exe").write_bytes(b"orphan")
    assert read_staged(tmp_path) is None


def test_read_staged_returns_none_when_platform_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Running on Windows but the manifest says this is a macOS stage. Can
    # happen if a user shares a data_dir across hosts (rare, but defensive).
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir()
    (staged_dir / "payload.exe").write_bytes(b"x")
    (staged_dir / MANIFEST_NAME).write_text(
        json.dumps({
            "version": "0.1.1",
            "platform": "macos",  # mismatch
            "sha256": "deadbeef",
            "notes": "",
            "ready_at": "2026-05-12T00:00:00Z",
        }),
        encoding="utf-8",
    )
    assert read_staged(tmp_path) is None


def test_read_staged_returns_none_when_manifest_unparseable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir()
    (staged_dir / "payload.exe").write_bytes(b"x")
    (staged_dir / MANIFEST_NAME).write_text("not-json{{{", encoding="utf-8")
    assert read_staged(tmp_path) is None


# ---------------------------------------------------------------------------
# clear_staged + re-entry
# ---------------------------------------------------------------------------


def test_clear_staged_removes_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    staged_dir = tmp_path / "staged_update"
    staged_dir.mkdir()
    (staged_dir / "payload.exe").write_bytes(b"x")
    (staged_dir / "payload.exe.partial").write_bytes(b"y")
    (staged_dir / MANIFEST_NAME).write_text("{}", encoding="utf-8")

    clear_staged(tmp_path)

    assert not (staged_dir / "payload.exe").exists()
    assert not (staged_dir / "payload.exe.partial").exists()
    assert not (staged_dir / MANIFEST_NAME).exists()


def test_clear_staged_is_safe_on_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    clear_staged(tmp_path)  # no error
    clear_staged(tmp_path / "missing")  # no error


async def test_clear_then_stage_replaces_previous_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Caller's "newer version available" path: clear the v0.1.1 stage then
    stage v0.1.2. The end state has only v0.1.2 on disk."""
    monkeypatch.setattr(sys, "platform", "win32")

    old_body = b"V-OLD"
    old_info = _info(old_body, version="0.1.1")
    async with httpx.AsyncClient(transport=_bytes_transport(old_body)) as client:
        await download_and_stage(old_info, tmp_path, client=client)

    assert read_staged(tmp_path) is not None
    clear_staged(tmp_path)
    assert read_staged(tmp_path) is None

    new_body = b"V-NEW"
    new_info = _info(new_body, version="0.1.2")
    async with httpx.AsyncClient(transport=_bytes_transport(new_body)) as client:
        new_staged = await download_and_stage(new_info, tmp_path, client=client)

    assert new_staged is not None
    assert new_staged.version == "0.1.2"
    assert new_staged.payload_path.read_bytes() == new_body


# ---------------------------------------------------------------------------
# Returned StagedUpdate shape (round-trip via read_staged)
# ---------------------------------------------------------------------------


async def test_staged_roundtrip_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    body = b"BYTES" * 200
    info = _info(body, version="2.8.1")
    async with httpx.AsyncClient(transport=_bytes_transport(body)) as client:
        from_download = await download_and_stage(info, tmp_path, client=client)
    from_read = read_staged(tmp_path)
    assert isinstance(from_download, StagedUpdate)
    assert isinstance(from_read, StagedUpdate)
    assert from_download.version == from_read.version
    assert from_download.platform == from_read.platform
    assert from_download.sha256 == from_read.sha256
    assert from_download.payload_path == from_read.payload_path
