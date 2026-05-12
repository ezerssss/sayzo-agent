"""Persist the version we last booted with, so the next launch can detect an
upgrade and fire the "Sayzo updated to vX.Y.Z" toast.

Stored at ``<data_dir>/last_seen_version.txt`` — one line, just the version
string. Atomic-written via temp + replace so a crash mid-write can't corrupt
the file. Reads strip whitespace.

Edge cases:
  - File missing -> ``None`` (first-ever install — no upgrade to celebrate).
  - File empty / whitespace-only -> ``None``.
  - Write fails -> log + swallow (auto-update telemetry must never block agent
    startup; we'll just miss one upgrade notification).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

LAST_SEEN_FILENAME = "last_seen_version.txt"


def _path(data_dir: Path) -> Path:
    return data_dir / LAST_SEEN_FILENAME


def read_last_seen(data_dir: Path) -> Optional[str]:
    """Return the persisted last-seen version, or ``None`` on any failure.

    A missing file is the first-ever-launch case and intentionally indistinct
    from an unreadable one — both resolve to "no upgrade toast on this boot".
    """
    path = _path(data_dir)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("[last-version] couldn't read %s", path, exc_info=True)
        return None
    if not text:
        return None
    # If the file got corrupted into multiple lines, take the first non-empty
    # one — we'd rather skip the toast on this boot than crash trying to parse
    # a malformed semver later.
    first_line = text.splitlines()[0].strip()
    return first_line or None


def write_last_seen(data_dir: Path, version: str) -> None:
    """Atomically persist ``version`` as the last-seen build.

    Best-effort: a write failure is logged and swallowed. The agent will retry
    on the next launch.
    """
    if not version:
        return
    path = _path(data_dir)
    tmp = path.with_suffix(".txt.tmp")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(version.strip() + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("[last-version] couldn't write %s", path, exc_info=True)
        # Best-effort cleanup of the tmp; ignore further errors.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
