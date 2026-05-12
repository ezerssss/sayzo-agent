"""Cross-platform dispatcher for applying a staged update.

Called from three places in ``__main__.py::service``:

1. **Boot-time** (after pidfile acquisition, before any heavy work). Handles
   the "user rebooted without quitting" case — launchd / Task Scheduler
   booted the OLD agent and we want to swap in the staged version before
   anything important starts.
2. **Quit-time** (after the asyncio main loop returns, before the final
   ``os._exit`` / ``remove_pid``). Normal apply path when the user clicks
   tray Quit while a stage is ready.
3. **Settings → Install update** (Stage 4, future). Same call, different
   trigger.

When a staged version is found that's strictly newer than the running
``__version__``, the platform-specific helper takes over (``Popen`` +
``os._exit``); this function never returns from that branch. If anything
fails — no stage on disk, stage isn't newer, spawn raises — the function
returns normally and the agent stays on its current version.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .update import is_newer
from .update_stage import read_staged

log = logging.getLogger(__name__)


def apply_staged_if_newer(data_dir: Path, current_version: str, *, where: str) -> None:
    """If a staged update is strictly newer than ``current_version``, hand off
    to the platform helper. Otherwise no-op.

    ``where`` is a short tag ("boot", "quit", "settings") that flows into the
    log line so support can tell which path triggered the apply.
    """
    staged = read_staged(data_dir)
    if staged is None:
        return
    if not is_newer(current_version, staged.version):
        # Stage exists but isn't newer than us — could be a stale leftover
        # from before we just upgraded (the post-upgrade detection in
        # __main__.py clears these). Don't apply; let the caller continue.
        return

    log.warning(
        "[update] applying staged v%s at %s (currently running v%s)",
        staged.version, where, current_version,
    )
    try:
        if sys.platform == "win32":
            from .update_apply_win import spawn_installer_and_exit

            spawn_installer_and_exit(staged)
        elif sys.platform == "darwin":
            from .update_apply_mac import spawn_swap_helper_and_exit

            spawn_swap_helper_and_exit(staged)
        # Unsupported platforms (Linux dev runs) drop through silently —
        # auto-update isn't promised there.
    except Exception:
        log.warning(
            "[update] apply at %s failed — staying on current version", where,
            exc_info=True,
        )
