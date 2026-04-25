"""Shared hotkey read/validate/save helpers for pywebview bridges.

Both the setup wizard and the Settings window expose ``get_hotkey``,
``validate_hotkey``, ``save_hotkey`` to JS with identical persistence
semantics: read from ``user_settings.json`` overlaid on
``ArmConfig.hotkey``, validate via :mod:`sayzo_agent.arm.hotkey`, save back
through ``settings_store``. The Settings bridge also nudges the live
``ArmController.rebind_hotkey`` over IPC so the new combo takes effect
without a restart — that nudge is layered on top of ``save_hotkey_binding``
by the Settings bridge itself.
"""
from __future__ import annotations

import logging
from typing import Any

from sayzo_agent import settings_store
from sayzo_agent.arm.hotkey import humanize_binding, validate_binding
from sayzo_agent.config import Config

log = logging.getLogger(__name__)


def get_hotkey(cfg: Config) -> dict[str, Any]:
    """Return the saved binding plus its human-readable form.

    Falls back to the ``ArmConfig`` default when nothing has been saved —
    matches the in-process arm controller's lookup so what the user sees
    in Settings is exactly what the global hotkey is bound to.
    """
    raw = settings_store.load(cfg.data_dir)
    binding = raw.get("arm", {}).get("hotkey") or cfg.arm.hotkey
    return {"binding": binding, "display": humanize_binding(binding)}


def validate_hotkey(binding: str) -> dict[str, Any]:
    """Run the shared validator (rejects bare keys, OS-reserved combos)."""
    return {"error": validate_binding(binding)}


def save_hotkey(cfg: Config, binding: str) -> dict[str, Any]:
    """Persist the binding to ``user_settings.json``.

    Validated first so we don't write garbage. A failed save returns the
    error and leaves disk state untouched. Callers that need the live
    ``ArmController`` to pick up the change immediately should layer an
    IPC ``rebind_hotkey`` call on top — the Settings bridge does this; the
    setup bridge runs in-process and doesn't need it.
    """
    err = validate_binding(binding)
    if err is not None:
        return {"error": err}
    try:
        settings_store.save(cfg.data_dir, {"arm": {"hotkey": binding}})
    except OSError as e:
        log.warning("[hotkey] save failed", exc_info=True)
        return {"error": f"Couldn't save: {e}"}
    return {"error": None, "display": humanize_binding(binding)}
