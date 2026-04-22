"""Update-availability check for the desktop agent (Phase A: notify-only).

Fetches a small public manifest from ``sayzo.app/releases/latest.json`` and
compares the advertised version against the running ``__version__``. When a
newer version exists, callers (the ``service`` command's background task)
surface a tray item + toast pointing the user at the platform-specific
installer URL.

Everything here is best-effort: network errors, bad JSON, and unparseable
versions all resolve to "no update available" — never raise. Auto-update must
never break the capture pipeline.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_MANIFEST_URL = "https://sayzo.app/releases/latest.json"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    notes: str


def platform_key() -> Optional[str]:
    """Manifest key matching the running platform, or None for unsupported OS."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return None


def _parse_version(s: str) -> Optional[tuple[int, ...]]:
    """Parse a dotted-numeric version string. Returns None on any failure.

    Handles the ``-dev`` / ``-rc1`` suffixes we use for non-release builds by
    stripping everything after the first non-numeric segment — a pre-release
    suffix always compares "older" to the next real release, which is what we
    want (a ``0.1.0-dev`` agent should upgrade to ``0.1.0``).
    """
    try:
        head = s.split("-", 1)[0]
        parts = tuple(int(p) for p in head.split("."))
    except (ValueError, AttributeError):
        return None
    return parts if parts else None


def is_newer(current: str, candidate: str) -> bool:
    """True iff ``candidate`` is strictly newer than ``current``.

    Fail-safe: if either string is unparseable we return False so the caller
    never surfaces a spurious "update available" toast to the user base.
    """
    c = _parse_version(current)
    n = _parse_version(candidate)
    if c is None or n is None:
        return False
    return n > c


async def fetch_manifest(
    client: httpx.AsyncClient, url: str = DEFAULT_MANIFEST_URL
) -> Optional[dict]:
    """GET the manifest and return its parsed JSON dict, or None on any error."""
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.warning("[update] manifest fetch failed from %s", url, exc_info=True)
        return None


async def check(
    current_version: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    url: str = DEFAULT_MANIFEST_URL,
) -> Optional[UpdateInfo]:
    """Return an :class:`UpdateInfo` if a newer build exists for this platform.

    Returns None when: current is up-to-date, the manifest can't be fetched or
    parsed, the running platform has no entry, or the advertised version is
    unparseable. Constructs its own ``httpx.AsyncClient`` if one isn't passed —
    the service's update-check task owns its lifecycle cleanly.
    """
    pkey = platform_key()
    if pkey is None:
        return None

    owned = client is None
    if owned:
        client = httpx.AsyncClient()
    try:
        data = await fetch_manifest(client, url)
    finally:
        if owned:
            await client.aclose()

    if not isinstance(data, dict):
        return None

    advertised = data.get("version")
    if not isinstance(advertised, str) or not is_newer(current_version, advertised):
        return None

    platform_entry = data.get(pkey)
    if not isinstance(platform_entry, dict):
        return None
    dl_url = platform_entry.get("url")
    if not isinstance(dl_url, str) or not dl_url:
        return None

    notes = data.get("notes") if isinstance(data.get("notes"), str) else ""
    return UpdateInfo(version=advertised, url=dl_url, notes=notes)
