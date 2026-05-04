"""On-disk cache of the most recent ``GET /api/me`` response.

The cache is the runtime source of truth for the arm-time gate. Network
refreshes write here; the gate reads here. Treating the cache as the source
of truth (rather than calling the server inline at every arm attempt) keeps
the user usable offline once we've ever observed a positive state.

Schema is versioned so a future change to the gate's semantics can force a
re-fetch by bumping :data:`CACHE_SCHEMA_VERSION` instead of trying to
migrate stale records.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from ..config import Config

log = logging.getLogger(__name__)

CACHE_FILENAME = "account_status.json"
CACHE_SCHEMA_VERSION = 1


# Just the persistable account states. Fetch-failure states (auth_required,
# transient_error, unknown_error) are NOT written to the cache — those are
# transient and the cache only records the last observed account state.
CachedAccountState = Literal[
    "ok",
    "onboarding_required",
    "suspended",
    "deleted",
]

# Single source of truth for runtime validation + gate decisions. Iterating
# the Literal isn't cheap or pretty; freeze the values once.
VALID_ACCOUNT_STATES: frozenset[str] = frozenset(
    ("ok", "onboarding_required", "suspended", "deleted")
)
BLOCKED_ACCOUNT_STATES: frozenset[str] = frozenset(
    ("onboarding_required", "suspended", "deleted")
)


@dataclass
class CachedAccountStatus:
    """Last observed account state, persisted across restarts."""

    account_state: CachedAccountState
    onboarding_complete: bool
    onboarding_url: Optional[str]
    email: Optional[str]
    user_id: Optional[str]
    fetched_at: str  # ISO 8601 UTC

    def fetched_at_dt(self) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def age_seconds(self, *, now: Optional[datetime] = None) -> Optional[float]:
        dt = self.fetched_at_dt()
        if dt is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - dt).total_seconds())


def cache_path(cfg: Config) -> Path:
    return cfg.data_dir / CACHE_FILENAME


def read_cache(cfg: Config) -> Optional[CachedAccountStatus]:
    """Return the cached account status, or ``None`` if missing / unreadable.

    Corrupt cache files are logged and treated as missing — the gate then
    treats the state as ``unknown`` and the next refresh repopulates.
    """
    path = cache_path(cfg)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("[account.cache] read failed for %s", path, exc_info=True)
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("[account.cache] %s contains invalid JSON; ignoring", path)
        return None
    if not isinstance(data, dict):
        log.warning("[account.cache] %s top-level is not an object; ignoring", path)
        return None

    version = data.get("version")
    if version != CACHE_SCHEMA_VERSION:
        log.info(
            "[account.cache] %s has version %r, expected %d — forcing refresh",
            path,
            version,
            CACHE_SCHEMA_VERSION,
        )
        return None

    account_state = data.get("account_state")
    if account_state not in VALID_ACCOUNT_STATES:
        log.warning(
            "[account.cache] %s has unknown account_state %r; ignoring",
            path,
            account_state,
        )
        return None

    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at:
        log.warning("[account.cache] %s missing fetched_at; ignoring", path)
        return None

    return CachedAccountStatus(
        account_state=account_state,  # type: ignore[arg-type]
        onboarding_complete=bool(data.get("onboarding_complete", False)),
        onboarding_url=_str_or_none(data.get("onboarding_url")),
        email=_str_or_none(data.get("email")),
        user_id=_str_or_none(data.get("user_id")),
        fetched_at=fetched_at,
    )


def write_cache(cfg: Config, cached: CachedAccountStatus) -> None:
    """Atomically write ``cached`` to ``account_status.json``.

    Uses temp-file + ``os.replace`` so a crash mid-write can't leave a
    partially-flushed file that ``read_cache`` would silently treat as
    missing.
    """
    path = cache_path(cfg)
    payload = {"version": CACHE_SCHEMA_VERSION, **asdict(cached)}
    serialized = json.dumps(payload, indent=2)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning(
            "[account.cache] failed to create parent dir for %s", path, exc_info=True
        )
        return

    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".account_status.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        log.warning("[account.cache] write failed for %s", path, exc_info=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return

    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def clear_cache(cfg: Config) -> None:
    path = cache_path(cfg)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        log.warning("[account.cache] clear failed for %s", path, exc_info=True)


def now_iso() -> str:
    """UTC timestamp formatted for the cache's ``fetched_at`` field."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _str_or_none(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
