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

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .update import is_newer
from .update_stage import STAGED_DIR_NAME, clear_staged, read_staged

log = logging.getLogger(__name__)

QUIT_APPLY_FLAG_NAME = "quit_with_apply.flag"

# Marker written by ``apply_staged_if_newer`` for any *user-initiated* apply
# (``where != "boot"``), persisted across the relaunch the platform helper
# triggers. The freshly-relaunched agent consumes it on boot to decide
# whether to re-open the Settings window (on About) — a user who clicked
# "Install update" wants to land back in Settings; a silent boot-time
# auto-apply must NOT pop Settings. Disambiguates the two paths, which
# otherwise look identical to the new agent (Windows passes ``--open-settings``
# for both; macOS's ``open --args service`` trips ``looks_user_launched`` for
# both). See ``__main__.py::service`` open-settings decision.
OPEN_SETTINGS_FLAG_NAME = "open_settings_after_update.flag"

# Cap on how many times we'll re-spawn the platform installer / swap helper
# for the SAME staged version before giving up. Prevents an unrecoverable
# boot-loop when the helper consistently fails (e.g. a bad DMG mount, perm
# error on /Applications, NSIS rejected on a managed Windows endpoint). A
# new staged version resets the counter — see :func:`_record_apply_attempt`.
MAX_APPLY_ATTEMPTS = 3
APPLY_ATTEMPTS_FILE = "apply_attempts.json"


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


def _open_settings_flag_path(data_dir: Path) -> Path:
    return data_dir / OPEN_SETTINGS_FLAG_NAME


def set_open_settings_after_update(data_dir: Path) -> None:
    """Mark that the next post-update boot should re-open Settings (on About).

    Written by :func:`apply_staged_if_newer` for user-initiated applies
    (``where != "boot"``). Best-effort: a failed write just means the new
    agent treats the update as silent (no Settings pop) — never breaks the
    apply itself.
    """
    path = _open_settings_flag_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        log.warning(
            "[update] failed to write open-settings-after-update intent at %s",
            path, exc_info=True,
        )


def take_open_settings_after_update(data_dir: Path) -> bool:
    """Consume the open-settings-after-update marker: return True iff it was
    present, deleting it either way.

    Consumed unconditionally on every boot so a stale marker (e.g. from a
    user-initiated apply whose helper spawn then failed) self-clears on the
    next boot rather than lingering until some future upgrade.
    """
    path = _open_settings_flag_path(data_dir)
    present = path.exists()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug(
            "[update] failed to clear open-settings-after-update intent at %s",
            path, exc_info=True,
        )
    return present


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

    Bounded by :data:`MAX_APPLY_ATTEMPTS` per staged version. Once a version
    has been attempted that many times without producing a successful upgrade
    (which the boot path detects via :mod:`.last_version` and clears the
    staged slot), the stage is cleared and a flag is left for the next boot
    to surface as a "Sayzo update failed" toast (see
    :func:`get_failed_apply_version`). Without the cap, a broken stage made
    the agent unbootable into its tray — every boot would re-spawn the
    helper, the helper would fail the same way, the user would see only
    "exiting agent for swap" before the agent vanished, and there was no
    in-app way out.
    """
    staged = read_staged(data_dir)
    if staged is None:
        return
    if not is_newer(current_version, staged.version):
        # Stage exists but isn't newer than us — could be a stale leftover
        # from before we just upgraded (the post-upgrade detection in
        # __main__.py clears these). Don't apply; let the caller continue.
        return

    attempts = _record_apply_attempt(data_dir, staged.version)
    if attempts > MAX_APPLY_ATTEMPTS:
        log.warning(
            "[update] apply for v%s exceeded %d attempts at %s; clearing "
            "staged slot and surfacing failure toast on next boot",
            staged.version, MAX_APPLY_ATTEMPTS, where,
        )
        # Drop the payload + manifest so the next boot doesn't re-enter this
        # path. Leaves apply_attempts.json in place — the boot path reads it
        # via get_failed_apply_version() to fire the user-visible toast and
        # then clears it via clear_apply_attempts(). The user can re-trigger
        # the install from Settings (which will re-stage from scratch and
        # reset the counter via the version-mismatch branch below).
        clear_staged(data_dir)
        return

    log.warning(
        "[update] applying staged v%s at %s (attempt %d/%d, currently running v%s)",
        staged.version, where, attempts, MAX_APPLY_ATTEMPTS, current_version,
    )
    # User-initiated applies (Settings → Install update, tray "Install…",
    # HUD "Install now" — all route through where="quit") leave a marker so
    # the relaunched agent re-opens Settings on About. The boot-time
    # auto-apply (where="boot") leaves nothing, so it stays silent (toast
    # only). "quit" is the only non-boot caller; see the flag's docstring.
    if where != "boot":
        set_open_settings_after_update(data_dir)
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


# ---------------------------------------------------------------------------
# Per-version apply-attempt tracking. Persisted at
# ``<data_dir>/staged_update/apply_attempts.json`` so the counter survives
# the agent restart that the swap helper triggers — without persistence we
# couldn't distinguish "this is the first attempt" from "the helper has
# failed twice in a row already" across boots.
# ---------------------------------------------------------------------------


def _attempts_path(data_dir: Path) -> Path:
    return data_dir / STAGED_DIR_NAME / APPLY_ATTEMPTS_FILE


def _read_attempts(data_dir: Path) -> dict:
    path = _attempts_path(data_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.debug("[update] apply_attempts unreadable at %s", path, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_attempts(data_dir: Path, payload: dict) -> None:
    path = _attempts_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.debug("[update] couldn't write apply_attempts at %s", path, exc_info=True)


def _record_apply_attempt(data_dir: Path, version: str) -> int:
    """Bump the attempt counter for ``version`` and return the new total.

    A version mismatch resets the counter — a fresh download is its own
    fresh slate, the previous version's failures don't penalize it.
    """
    payload = _read_attempts(data_dir)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if payload.get("version") != version:
        payload = {"version": version, "attempts": 0, "first_attempt_at": now}
    payload["attempts"] = int(payload.get("attempts", 0) or 0) + 1
    payload["last_attempt_at"] = now
    _write_attempts(data_dir, payload)
    return int(payload["attempts"])


def get_failed_apply_version(data_dir: Path) -> Optional[str]:
    """Return the version whose apply attempts exceeded :data:`MAX_APPLY_ATTEMPTS`,
    or ``None`` if no failed attempt is on record.

    Boot path uses this to fire a user-visible toast nudging the user to
    download manually from sayzo.app — the in-app retry path is failing
    consistently and continuing to silently retry every boot is worse than
    saying so out loud. Caller is expected to consume the marker via
    :func:`clear_apply_attempts` once the toast has fired.
    """
    payload = _read_attempts(data_dir)
    version = payload.get("version")
    attempts = payload.get("attempts", 0)
    if (isinstance(version, str) and version
            and isinstance(attempts, int)
            and attempts >= MAX_APPLY_ATTEMPTS):
        return version
    return None


def clear_apply_attempts(data_dir: Path) -> None:
    """Remove the apply-attempts record. Safe when missing."""
    path = _attempts_path(data_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("[update] failed to clear apply_attempts at %s", path, exc_info=True)
