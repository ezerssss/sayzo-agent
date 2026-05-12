"""Phase B staging: download a new release in the background and integrity-check it.

The update-check task in ``__main__.py`` runs this after ``update.check`` reports a
newer version. We stream the platform installer (Windows ``.exe`` or macOS
``.dmg``) into ``<data_dir>/staged_update/payload.<ext>``, hash the bytes along
the way, and only mark the staged release "ready" when SHA256 matches the
manifest. At the next agent restart the apply step (Stage 2) reads the staged
file and hands it to the platform-specific installer / swap helper.

Atomicity rules — kept simple so a half-written stage can't be mistaken for a
ready one:

  1. Download to ``payload.<ext>.partial``. The ``.partial`` suffix is invisible
     to :func:`read_staged`.
  2. Compute SHA256 along the byte stream. If it doesn't match the manifest's
     hash, delete the partial and return None.
  3. Rename ``.partial`` -> ``payload.<ext>``.
  4. Only AFTER the rename, write ``manifest.json``. :func:`read_staged`
     requires BOTH files; a payload without a manifest reads as "no stage".

A failure at any step leaves the previous stage (if any) untouched until step
3 — so a flaky network during step 1 doesn't blow away a working v2.8.1 stage
while attempting v2.8.2. Callers who want to replace an older stage with a
newer one call :func:`clear_staged` first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .update import UpdateInfo, platform_key

log = logging.getLogger(__name__)

STAGED_DIR_NAME = "staged_update"
MANIFEST_NAME = "manifest.json"


def _payload_extension() -> Optional[str]:
    pkey = platform_key()
    if pkey == "windows":
        return "exe"
    if pkey == "macos":
        return "dmg"
    return None


def _staged_dir(data_dir: Path) -> Path:
    return data_dir / STAGED_DIR_NAME


def _payload_path(data_dir: Path, ext: str) -> Path:
    return _staged_dir(data_dir) / f"payload.{ext}"


def _manifest_path(data_dir: Path) -> Path:
    return _staged_dir(data_dir) / MANIFEST_NAME


@dataclass(frozen=True)
class StagedUpdate:
    """A staged release that's ready to apply at next agent restart."""

    version: str
    platform: str
    sha256: str
    notes: str
    payload_path: Path
    ready_at: str


async def download_and_stage(
    info: UpdateInfo,
    data_dir: Path,
    *,
    client: Optional[httpx.AsyncClient] = None,
    chunk_size: int = 64 * 1024,
) -> Optional[StagedUpdate]:
    """Download ``info.url`` to a staging slot and verify SHA256.

    Returns a :class:`StagedUpdate` on success, ``None`` on any failure
    (network error, hash mismatch, unsupported platform, disk error). Failures
    never raise — auto-update must not break capture.

    The caller is responsible for not re-entering this function while a stage
    is in flight. The update-check task in ``__main__.py`` runs serially, so
    overlap is impossible in production; tests should still be careful.
    """
    ext = _payload_extension()
    pkey = platform_key()
    if ext is None or pkey is None:
        return None

    staged_dir = _staged_dir(data_dir)
    try:
        staged_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("[update-stage] could not create %s", staged_dir, exc_info=True)
        return None

    final = _payload_path(data_dir, ext)
    partial = final.with_name(final.name + ".partial")

    # If a previous run left a partial behind (crash mid-download), wipe it
    # before we start so the new download isn't appended to stale bytes.
    if partial.exists():
        try:
            partial.unlink()
        except OSError:
            log.warning(
                "[update-stage] couldn't remove stale partial %s", partial,
                exc_info=True,
            )

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    hasher = hashlib.sha256()
    total_bytes = 0
    next_log_threshold = 0.05  # log at 5%, 10%, ... so a long download is visible
    try:
        async with client.stream("GET", info.url) as resp:
            resp.raise_for_status()
            content_length_header = resp.headers.get("content-length")
            try:
                expected_total = int(content_length_header) if content_length_header else 0
            except (TypeError, ValueError):
                expected_total = 0

            with open(partial, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size):
                    fh.write(chunk)
                    hasher.update(chunk)
                    total_bytes += len(chunk)
                    if expected_total > 0:
                        ratio = total_bytes / expected_total
                        if ratio >= next_log_threshold and next_log_threshold < 1.0:
                            log.info(
                                "[update-stage] downloading v%s: %.0f%% (%d / %d bytes)",
                                info.version, ratio * 100,
                                total_bytes, expected_total,
                            )
                            next_log_threshold += 0.05
    except Exception:
        log.warning(
            "[update-stage] download failed for v%s from %s",
            info.version, info.url, exc_info=True,
        )
        _safe_unlink(partial)
        return None
    finally:
        if owned:
            await client.aclose()

    got_hex = hasher.hexdigest()
    if got_hex.lower() != info.sha256.lower():
        log.warning(
            "[update-stage] sha256 mismatch for v%s — expected=%s got=%s (%d bytes); discarding",
            info.version, info.sha256, got_hex, total_bytes,
        )
        _safe_unlink(partial)
        return None

    try:
        partial.replace(final)
    except OSError:
        log.warning(
            "[update-stage] couldn't finalize %s -> %s", partial, final,
            exc_info=True,
        )
        _safe_unlink(partial)
        return None

    ready_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "version": info.version,
        "platform": pkey,
        "sha256": info.sha256.lower(),
        "notes": info.notes,
        "ready_at": ready_at,
    }
    manifest_path = _manifest_path(data_dir)
    try:
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        manifest_tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_tmp.replace(manifest_path)
    except OSError:
        # Payload landed but manifest didn't — read_staged will treat this as
        # "no stage" so the next check cycle re-stages cleanly.
        log.warning(
            "[update-stage] couldn't write manifest %s", manifest_path,
            exc_info=True,
        )
        _safe_unlink(final)
        return None

    log.info(
        "[update-stage] staged v%s (%d bytes) -> %s",
        info.version, total_bytes, final,
    )
    return StagedUpdate(
        version=info.version,
        platform=pkey,
        sha256=info.sha256.lower(),
        notes=info.notes,
        payload_path=final,
        ready_at=ready_at,
    )


def read_staged(data_dir: Path) -> Optional[StagedUpdate]:
    """Return the currently-staged update, or None if nothing's ready.

    Requires BOTH ``manifest.json`` and ``payload.<ext>`` to exist and parse
    cleanly. Any inconsistency (manifest unreadable, payload missing, platform
    field doesn't match the running platform) resolves to None — auto-update
    skips that cycle rather than misapplying.
    """
    ext = _payload_extension()
    if ext is None:
        return None

    manifest_path = _manifest_path(data_dir)
    payload_path = _payload_path(data_dir, ext)
    if not manifest_path.is_file() or not payload_path.is_file():
        return None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("[update-stage] manifest unreadable at %s", manifest_path, exc_info=True)
        return None
    if not isinstance(manifest, dict):
        return None

    version = manifest.get("version")
    platform_name = manifest.get("platform")
    sha256 = manifest.get("sha256")
    notes = manifest.get("notes") if isinstance(manifest.get("notes"), str) else ""
    ready_at = manifest.get("ready_at") if isinstance(manifest.get("ready_at"), str) else ""

    if not isinstance(version, str) or not version:
        return None
    if not isinstance(platform_name, str) or platform_name != platform_key():
        return None
    if not isinstance(sha256, str) or not sha256:
        return None

    return StagedUpdate(
        version=version,
        platform=platform_name,
        sha256=sha256,
        notes=notes,
        payload_path=payload_path,
        ready_at=ready_at,
    )


def clear_staged(data_dir: Path) -> None:
    """Remove any staged payload + manifest. Safe to call when nothing's staged."""
    ext = _payload_extension()
    if ext is None:
        return
    staged_dir = _staged_dir(data_dir)
    if not staged_dir.is_dir():
        return
    for name in (f"payload.{ext}", f"payload.{ext}.partial", MANIFEST_NAME):
        _safe_unlink(staged_dir / name)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("[update-stage] couldn't unlink %s", path, exc_info=True)
