"""Cross-process single-instance lock for the Settings subprocess.

Two ``sayzo-agent settings`` invocations can race independently — the
tray's "Open Settings" click while a window is already open, or a user
double-clicking the tray menu. This module owns the cross-process
single-instance guard for that subprocess.

Uses the same kernel-lock primitive as the agent service
(``sayzo_agent.pidfile``) so the Settings GUI inherits the same
robustness properties: kernel auto-releases on process death (clean
exit, kill, BSOD, reboot), no stale userspace state possible.

The pidfile at ``data_dir/settings.pid`` is informational — it stores
the PID of the active Settings window so callers can read it for
diagnostics. The actual lock is held in the kernel (named mutex on
Windows, ``fcntl.flock`` on Unix); the .pid file is just a sticky note.

Usage::

    with SettingsLock(cfg.data_dir) as lock:
        if not lock.acquired:
            return  # another Settings window is already open
        # ... open the pywebview window ...
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sayzo_agent import pidfile

log = logging.getLogger(__name__)

_LOCK_FILENAME = "settings.pid"


class SettingsLock:
    """Context manager for the Settings single-instance lock.

    On enter, attempts to acquire the kernel lock. If another Settings
    process holds it, ``acquired`` is False and the caller should bail.
    On exit, releases the kernel lock and removes the .pid file iff we
    own it. The kernel auto-releases on abnormal termination, so a
    crashed Settings process never blocks the next launch.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _LOCK_FILENAME
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def path(self) -> Path:
        return self._path

    def existing_pid(self) -> Optional[int]:
        """Read the PID currently in the .pid file, or None.

        Informational: the lock itself is in the kernel, but callers
        sometimes want the active primary's PID (e.g. for log output
        or to send a focus-window IPC message).
        """
        try:
            return int(self._path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def __enter__(self) -> "SettingsLock":
        self._acquired = pidfile.try_acquire_pidfile(self._path)
        if not self._acquired:
            prior = self.existing_pid()
            if prior is not None:
                log.info(
                    "[settings.lock] another Settings window is open (pid=%d)",
                    prior,
                )
            else:
                log.info("[settings.lock] another Settings window holds the lock")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._acquired:
            return
        pidfile.remove_pid(self._path)
        self._acquired = False
