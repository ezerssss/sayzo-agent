"""Cross-platform dispatcher for applying a staged update.

Called from three places in ``__main__.py::service``:

1. **Boot-time** (after pidfile acquisition, before any heavy work). Handles
   the "user rebooted without quitting" case — launchd / Task Scheduler
   booted the OLD agent and we want to swap in the staged version before
   anything important starts. Unconditional — boot always applies whatever's
   on disk.
2. **Quit-time** (after the asyncio main loop returns, before the final
   ``os._exit`` / ``remove_pid``). Gated on the explicit
   :data:`QUIT_APPLY_FLAG_NAME` intent flag: Settings → Install update, the
   tray "Install Sayzo vX.Y.Z" menu item, and the HUD "Install now" toast
   button all write the flag before triggering the quit. A plain tray Quit
   does NOT write the flag, so the agent quits cleanly and the stage applies
   on the next launch via the boot path.
3. **Settings → Install update** — same quit-time path, just driven from
   the Settings subprocess.

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

QUIT_APPLY_FLAG_NAME = "quit_with_apply.flag"


def _flag_path(data_dir: Path) -> Path:
    return data_dir / QUIT_APPLY_FLAG_NAME


def set_quit_apply_intent(data_dir: Path) -> None:
    """Mark the next agent quit as an explicit apply-the-staged-update quit.

    Written by Settings → Install update, the tray "Install Sayzo vX.Y.Z"
    click, and the HUD "Install now" toast button — all the surfaces that
    explicitly invite the user to swap the binary right now. The quit-time
    apply path consumes the flag; boot-time clears any stale flag from a
    prior session.

    Best-effort: any OSError logs + returns without raising. Failing to
    write the flag falls back to "stage sits, applies on next launch via
    the boot path" — never breaks capture.
    """
    path = _flag_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        log.warning("[update] failed to write quit-apply intent at %s", path, exc_info=True)


def has_quit_apply_intent(data_dir: Path) -> bool:
    """Return True if a quit-apply intent flag is currently on disk.

    Pure existence check, no side effects — safe for the pre-quit toast
    hook to peek without consuming the flag (the quit-time apply path
    explicitly clears it before calling into the platform helper).
    """
    return _flag_path(data_dir).exists()


def clear_quit_apply_intent(data_dir: Path) -> None:
    """Remove the quit-apply intent flag if present. Safe when absent."""
    path = _flag_path(data_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("[update] failed to clear quit-apply intent at %s", path, exc_info=True)


def apply_staged_at_quit_if_flagged(data_dir: Path, current_version: str) -> None:
    """Apply a staged update at quit time, but only when the user explicitly
    asked for it via the quit-apply intent flag.

    Consumes the flag before handing off so a no-op apply (no stage on disk,
    or stage isn't newer) doesn't auto-fire on the NEXT quit.
    """
    if not has_quit_apply_intent(data_dir):
        return
    clear_quit_apply_intent(data_dir)
    apply_staged_if_newer(data_dir, current_version, where="quit")


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
