"""Persistent user-editable settings (hotkey, notification toggles, etc.).

Lives at ``data_dir / user_settings.json``. The Settings window and first-run
onboarding write to this file; :func:`load_config` overlays it onto the
``ArmConfig`` defaults so user-chosen values survive restarts. Environment
variables still take precedence over what's saved here, so dev overrides
continue to work.

The file format is forward-compatible — unknown keys are kept on write
(read-modify-write) so older agent builds don't strip forward-added fields.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FILENAME = "user_settings.json"


def settings_path(data_dir: Path) -> Path:
    return data_dir / _FILENAME


def load(data_dir: Path) -> dict[str, Any]:
    """Read the user-settings JSON. Returns {} on missing/malformed file.

    Bad JSON is logged and treated as empty rather than raising — a corrupt
    settings file should not prevent the agent from booting. The user can
    re-save from the Settings window to heal it.
    """
    path = settings_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("[settings] failed to read %s", path, exc_info=True)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[settings] malformed JSON in %s; ignoring", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        log.warning("[settings] %s did not decode to an object; ignoring", path)
        return {}
    return data


def save(data_dir: Path, patch: dict[str, Any]) -> None:
    """Merge `patch` into the on-disk settings and atomically rewrite.

    Merge is shallow at the top level and recursive one level deep (so
    ``{"arm": {"hotkey": "..."}}`` merges into the existing ``arm`` block
    without clobbering sibling keys). Unknown top-level keys are preserved.
    Write is temp-file + replace to survive crashes mid-write.
    """
    path = settings_path(data_dir)
    current = load(data_dir)
    merged = _merge(current, patch)
    _write(path, merged, data_dir)


def replace(data_dir: Path, document: dict[str, Any]) -> None:
    """Overwrite the entire user-settings document atomically.

    Use this when a merge is the wrong semantics — most commonly to
    *delete* a nested key (Settings → Meeting Apps → Reset clears
    ``arm.detectors`` so the shipping defaults reappear; merge-based
    saves can't represent a deletion since missing keys mean
    "preserve"). Passes the document through verbatim, so callers are
    responsible for read-modify-write.
    """
    _write(settings_path(data_dir), document, data_dir)


def _write(path: Path, document: dict[str, Any], data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _merge(dst: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(dst)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out
