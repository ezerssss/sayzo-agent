"""Cross-process single-instance lock for the Settings subprocess.

The legacy in-process tkinter Settings used a ``threading.Event`` to prevent
double-clicks on the tray menu from spawning two windows. That guard does
not generalise to a subprocess model — two ``sayzo-agent settings`` processes
can race independently. This module owns the cross-process equivalent: a
PID file at ``data_dir/settings.pid`` plus a stale-detection check.

Usage::

    with SettingsLock(cfg.data_dir) as lock:
        if not lock.acquired:
            return  # another Settings window is already open
        # ... open the pywebview window ...
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_LOCK_FILENAME = "settings.pid"


def _is_pid_alive(pid: int) -> bool:
    """Best-effort liveness check, robust to cross-privilege contexts.

    On Windows, ``os.kill(pid, 0)`` raises ``PermissionError`` when the
    target lives at a higher integrity level (e.g. an elevated Settings
    spawned by NSIS finish-page vs. a user-launched Settings). Treating
    that as "dead" lets a second instance come up alongside the first.
    ``psutil.pid_exists`` queries the OS without ``PROCESS_ALL_ACCESS``,
    so it returns True correctly across the elevation boundary.
    """
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class SettingsLock:
    """Context manager for the Settings single-instance PID file.

    On enter, attempts to write our PID to ``data_dir/settings.pid``. If the
    file already exists with a live PID, ``acquired`` is False and the caller
    should bail. If the file exists but the PID is stale (process gone), we
    overwrite it and proceed. On exit, removes the file iff we own it.
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
        """Read the PID currently in the lockfile, or None."""
        try:
            return int(self._path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def __enter__(self) -> "SettingsLock":
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("[settings.lock] data_dir not writable: %s", self._path.parent)
            self._acquired = False
            return self

        # Atomic create-or-fail with O_EXCL to close the TOCTOU window
        # between "no live lockfile" and "we wrote ours". Two concurrent
        # Settings subprocess spawns now race at the OS level — exactly
        # one wins.
        payload = str(os.getpid()).encode("utf-8")
        for _attempt in range(2):
            try:
                fd = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                prior = self.existing_pid()
                if prior is not None and prior != os.getpid() and _is_pid_alive(prior):
                    log.info(
                        "[settings.lock] another Settings window is open (pid=%d)",
                        prior,
                    )
                    self._acquired = False
                    return self
                # Stale — remove and retry the exclusive create exactly once.
                try:
                    self._path.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "[settings.lock] failed to clear stale lock", exc_info=True
                    )
                    self._acquired = False
                    return self
                continue
            except OSError:
                log.warning(
                    "[settings.lock] failed to create %s", self._path, exc_info=True
                )
                self._acquired = False
                return self

            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            self._acquired = True
            return self

        # Lost the race even after the stale-clear retry.
        self._acquired = False
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._acquired:
            return
        try:
            current = self.existing_pid()
            if current == os.getpid():
                self._path.unlink(missing_ok=True)
        except OSError:
            log.debug("[settings.lock] cleanup failed", exc_info=True)
        self._acquired = False
