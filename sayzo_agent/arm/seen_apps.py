"""Persistence for unmatched mic-holders observed by the whitelist watcher.

When the user is disarmed and an app holds their microphone that is not on
the whitelist, we note it here. The Meeting Apps settings pane shows these
as "Suggested to add" so the user can one-click-add an app they actually
use — without having to know its process name or bundle id.

Data shape (``data_dir/seen_apps.json``):

    {
      "version": 1,
      "entries": [
        {
          "key": "loom.exe",
          "display_name": "Loom",
          "platform": "win32",
          "process_name": "loom.exe",
          "bundle_id": null,
          "first_seen_ts": 1713972611.42,
          "last_seen_ts":  1713973014.08,
          "seen_count": 3
        },
        ...
      ],
      "dismissed_keys": ["obs64.exe", "com.obsproject.obs-studio"]
    }

Design choices:

- **Cap at** ``_MAX_ENTRIES`` **(20)** — evict by oldest ``last_seen_ts``.
  Without a cap this file grows unboundedly; the user-facing section in
  Settings only needs a handful anyway.
- **Key is the lower-cased process name (Windows) or bundle id (macOS).**
  Same app observed via different paths collapses to one entry.
- **Whitelist scrubbing** — :func:`load` drops any entries that already
  match a spec in the passed-in whitelist. That way the Suggested pane
  never shows an app the user already has a detector for.
- **Dismiss is permanent** — :func:`dismiss` adds the key to
  ``dismissed_keys`` in addition to removing the entry, so the same app
  never bubbles up again even after a restart. The user can still add
  the app manually via the Add-app dialog (which also clears the
  dismissal so the app's future observations persist normally).
- **Forward-compat** — the reader is tolerant: unknown top-level keys are
  preserved on read-modify-write, ``version`` mismatch → treat as empty.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ..config import DetectorSpec

log = logging.getLogger(__name__)

_FILENAME = "seen_apps.json"
_SCHEMA_VERSION = 1
_MAX_ENTRIES = 20


@dataclass
class SeenApp:
    """One process observed holding the mic without matching the whitelist."""

    key: str
    display_name: str
    platform: str
    first_seen_ts: float
    last_seen_ts: float
    seen_count: int = 1
    process_name: Optional[str] = None
    bundle_id: Optional[str] = None


def _path(data_dir: Path) -> Path:
    return data_dir / _FILENAME


def _already_whitelisted(key: str, whitelist: Iterable[DetectorSpec]) -> bool:
    """True if the lower-cased key matches any process / bundle id across
    the whitelist. Both enabled and disabled specs count — a disabled spec
    is still "present" and the Suggested section shouldn't re-offer it."""
    key_lc = key.lower()
    for spec in whitelist:
        for p in spec.process_names:
            if p.lower() == key_lc:
                return True
        for b in spec.bundle_ids:
            if b.lower() == key_lc:
                return True
    return False


def _read_raw(data_dir: Path) -> dict:
    """Read and validate the JSON document. Returns an empty skeleton on
    missing / malformed / version-mismatched file."""
    path = _path(data_dir)
    empty: dict = {"version": _SCHEMA_VERSION, "entries": [], "dismissed_keys": []}
    if not path.exists():
        return empty
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.debug("[seen_apps] read failed", exc_info=True)
        return empty
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[seen_apps] malformed JSON; treating as empty", exc_info=True)
        return empty
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return empty
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    if not isinstance(data.get("dismissed_keys"), list):
        data["dismissed_keys"] = []
    return data


def _dismissed_set(data: dict) -> set[str]:
    return {
        str(k).lower()
        for k in data.get("dismissed_keys", [])
        if isinstance(k, str) and k
    }


def load(data_dir: Path, whitelist: Iterable[DetectorSpec]) -> list[SeenApp]:
    """Read seen-apps from disk, filtering out any that are now whitelisted
    or have been permanently dismissed.

    Returns an empty list on missing/malformed file or version mismatch —
    a corrupt file should never block the UI. Entries are returned sorted
    by ``last_seen_ts`` descending (most-recent first).
    """
    data = _read_raw(data_dir)
    entries_raw = data["entries"]
    dismissed = _dismissed_set(data)

    out: list[SeenApp] = []
    whitelist_list = list(whitelist)
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        if key.lower() in dismissed:
            continue
        if _already_whitelisted(key, whitelist_list):
            continue
        try:
            out.append(SeenApp(
                key=key,
                display_name=str(item.get("display_name") or key),
                platform=str(item.get("platform") or sys.platform),
                first_seen_ts=float(item.get("first_seen_ts", time.time())),
                last_seen_ts=float(item.get("last_seen_ts", time.time())),
                seen_count=int(item.get("seen_count", 1)),
                process_name=item.get("process_name"),
                bundle_id=item.get("bundle_id"),
            ))
        except (TypeError, ValueError):
            log.debug("[seen_apps] skip malformed entry: %r", item, exc_info=True)
            continue
    out.sort(key=lambda e: e.last_seen_ts, reverse=True)
    return out


def _save(data_dir: Path, entries: list[SeenApp], dismissed: Iterable[str]) -> None:
    """Atomically rewrite the on-disk list with ``entries`` +
    ``dismissed_keys``."""
    path = _path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Preserve insertion order + dedup of dismissed keys (JSON-stable).
    seen: set[str] = set()
    dismissed_unique: list[str] = []
    for k in dismissed:
        lk = str(k).lower()
        if not lk or lk in seen:
            continue
        seen.add(lk)
        dismissed_unique.append(lk)
    payload = {
        "version": _SCHEMA_VERSION,
        "entries": [asdict(e) for e in entries],
        "dismissed_keys": dismissed_unique,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record(
    data_dir: Path,
    *,
    key: str,
    display_name: str,
    whitelist: Iterable[DetectorSpec],
    process_name: Optional[str] = None,
    bundle_id: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> None:
    """Record one observation of a mic-holder that did not match any spec.

    Dedups by ``key`` — a repeat observation bumps ``last_seen_ts`` and
    ``seen_count`` instead of creating a duplicate row. Skips the write
    entirely if the key is already on the whitelist OR has been
    permanently dismissed. Caps the list at ``_MAX_ENTRIES`` by evicting
    the oldest ``last_seen_ts``.
    """
    if not key:
        return
    whitelist_list = list(whitelist)
    if _already_whitelisted(key, whitelist_list):
        return

    data = _read_raw(data_dir)
    dismissed = _dismissed_set(data)
    key_lc = key.lower()
    if key_lc in dismissed:
        # User said no — don't bring it back.
        return

    existing = load(data_dir, whitelist_list)
    now = time.time() if now_ts is None else now_ts

    found = False
    for e in existing:
        if e.key.lower() == key_lc:
            e.last_seen_ts = now
            e.seen_count += 1
            # Refresh display name / process / bundle in case our heuristic
            # improved — e.g., psutil finally returned a friendly exe name.
            if display_name:
                e.display_name = display_name
            if process_name:
                e.process_name = process_name
            if bundle_id:
                e.bundle_id = bundle_id
            found = True
            break

    if not found:
        existing.append(SeenApp(
            key=key_lc,
            display_name=display_name or key,
            platform=sys.platform,
            first_seen_ts=now,
            last_seen_ts=now,
            seen_count=1,
            process_name=process_name,
            bundle_id=bundle_id,
        ))

    # Evict oldest until we're under the cap. Keeps the file bounded and the
    # Suggested section in Settings from becoming a noisy wall.
    existing.sort(key=lambda e: e.last_seen_ts, reverse=True)
    if len(existing) > _MAX_ENTRIES:
        existing = existing[:_MAX_ENTRIES]

    try:
        _save(data_dir, existing, dismissed)
    except Exception:
        log.debug("[seen_apps] save failed", exc_info=True)


def dismiss(data_dir: Path, key: str) -> None:
    """Permanently dismiss ``key`` — used when the user clicks Dismiss in
    the Suggested section.

    Removes the entry from the rolling list AND records the key in
    ``dismissed_keys`` so future observations of the same app are
    suppressed (see :func:`record`) and :func:`load` filters it out on
    read. The user can still add the app via the Add-app dialog —
    :func:`undismiss` is called from the add flow to clear the marker so
    the app's observations persist normally thereafter.
    """
    if not key:
        return
    data = _read_raw(data_dir)
    key_lc = key.lower()

    # Drop from entries.
    entries_raw = data.get("entries", [])
    new_entries_raw = [
        it for it in entries_raw
        if not (isinstance(it, dict) and str(it.get("key", "")).lower() == key_lc)
    ]

    # Add to dismissed_keys (dedup).
    dismissed = _dismissed_set(data)
    dismissed.add(key_lc)

    # Rehydrate remaining entries so _save gets typed data.
    kept: list[SeenApp] = []
    for item in new_entries_raw:
        if not isinstance(item, dict):
            continue
        k = item.get("key")
        if not isinstance(k, str) or not k:
            continue
        try:
            kept.append(SeenApp(
                key=k,
                display_name=str(item.get("display_name") or k),
                platform=str(item.get("platform") or sys.platform),
                first_seen_ts=float(item.get("first_seen_ts", time.time())),
                last_seen_ts=float(item.get("last_seen_ts", time.time())),
                seen_count=int(item.get("seen_count", 1)),
                process_name=item.get("process_name"),
                bundle_id=item.get("bundle_id"),
            ))
        except (TypeError, ValueError):
            continue

    try:
        _save(data_dir, kept, dismissed)
    except Exception:
        log.debug("[seen_apps] dismiss save failed", exc_info=True)


def undismiss(data_dir: Path, key: str) -> None:
    """Clear a previously-dismissed key so the app's observations can
    accumulate again.

    Called from the Add-app flow when the user adds the same app they'd
    previously dismissed — if they changed their mind enough to add it,
    we shouldn't suppress future suggestions for it after they later
    remove it.
    """
    if not key:
        return
    data = _read_raw(data_dir)
    key_lc = key.lower()
    dismissed = _dismissed_set(data)
    if key_lc not in dismissed:
        return
    dismissed.discard(key_lc)

    # Rehydrate entries (unchanged) and rewrite.
    entries_raw = data.get("entries", [])
    kept: list[SeenApp] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        k = item.get("key")
        if not isinstance(k, str) or not k:
            continue
        try:
            kept.append(SeenApp(
                key=k,
                display_name=str(item.get("display_name") or k),
                platform=str(item.get("platform") or sys.platform),
                first_seen_ts=float(item.get("first_seen_ts", time.time())),
                last_seen_ts=float(item.get("last_seen_ts", time.time())),
                seen_count=int(item.get("seen_count", 1)),
                process_name=item.get("process_name"),
                bundle_id=item.get("bundle_id"),
            ))
        except (TypeError, ValueError):
            continue

    try:
        _save(data_dir, kept, dismissed)
    except Exception:
        log.debug("[seen_apps] undismiss save failed", exc_info=True)


def _display_name_for_process(proc_name: str) -> str:
    """Pretty-print a Windows process name (``zoom.exe`` → ``Zoom``).

    Strips ``.exe`` and title-cases the stem. Heuristic — hand-tuned
    display names from the default whitelist always win, this is only a
    fallback for never-before-seen apps.
    """
    stem = proc_name.rsplit(".", 1)[0] if "." in proc_name else proc_name
    stem = stem.replace("-", " ").replace("_", " ").strip()
    if not stem:
        return proc_name
    # Use title() for multi-word stems; leave single-letter-heavy names alone
    # so "MS Teams" doesn't become "Ms Teams" etc.
    if " " in stem:
        return stem.title()
    # Single word: capitalize first letter only.
    return stem[:1].upper() + stem[1:]


def _display_name_for_bundle(bundle_id: str) -> str:
    """Best-effort pretty name from a macOS bundle id.

    ``com.microsoft.teams`` → ``Teams``; ``us.zoom.xos`` → ``Xos`` (not
    great, but this is only the fallback for unknown apps — shipped
    detectors all have hand-tuned names).
    """
    parts = bundle_id.split(".")
    tail = parts[-1] if parts else bundle_id
    tail = tail.replace("-", " ").replace("_", " ").strip()
    if not tail:
        return bundle_id
    if " " in tail:
        return tail.title()
    return tail[:1].upper() + tail[1:]
