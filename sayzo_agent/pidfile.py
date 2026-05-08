"""Cross-platform single-instance enforcement via kernel-level locks.

Replaces the legacy pidfile-as-liveness pattern (v2.7.0 and earlier).

Why we moved away from .pid-file-as-lock
-----------------------------------------

A ``.pid`` file is just bytes on disk. Nothing about it is "live": when
the holder process dies, the file stays. Userspace heuristics ("is the
PID in psutil?", "did the file mtime predate boot?") each leak a
different edge case — the painful one being post-reboot PID recycling,
where Windows hands the dead process's PID to an unrelated process
(svchost, dwm, ...). The recycled-PID-as-alive false positive locks
every subsequent Sayzo launch out: Task Scheduler runs at logon, hits
``service already running``, exits silently. Same for every Start-menu
click after that. v2.1.18 + v2.1.19 patched two failure modes of the
.pid-file approach; v2.7.0 hit the third (post-reboot recycling) and
prompted this rewrite.

Kernel locks have none of those problems. The OS owns the lock state.
When the holder process exits — clean exit, force-kill, BSOD, reboot —
the kernel auto-releases the lock. There is no userspace state for
staleness to live in.

Implementation
--------------

Windows: ``CreateMutexW`` in the ``Local\\`` namespace (per Windows
login session, which is the right granularity for a per-user agent).
The mutex object is reference-counted by handle; releasing all handles
(process death does this automatically) destroys the mutex. We derive
the mutex name from a hash of the absolute pidfile path so independent
installs (production install vs. dev tree, two test ``tmp_path``s) get
independent locks.

macOS / Linux: ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` on the pidfile
itself. The kernel auto-releases the lock when the fd closes (process
death does this). flock is per-OFD on Linux and BSD-equivalent on
macOS — every fresh ``os.open`` is an independent contender.

The pidfile
-----------

The .pid file is still written (after a successful kernel acquire),
but is purely *informational* — it tells external tools / log readers
which PID is the current primary, and the IPC handoff path uses
``ipc.port`` (separate file) for routing. Liveness checks
(``is_running``) consult the kernel lock directly, not the pidfile
contents, so a stale .pid file from a previous boot session is
harmless: the next ``try_acquire_pidfile`` overwrites it.

Public API (back-compat with the v2.7.0 module shape)
-----------------------------------------------------

``try_acquire_pidfile(pid_path) -> bool``
    Acquire the kernel lock and overwrite the .pid file with our PID.
    True if we became primary; False if another live instance holds
    the lock. Non-reentrant: the same process should call this once at
    startup and never again until ``remove_pid``.

``remove_pid(pid_path) -> None``
    Release the kernel lock and remove the .pid file. Idempotent.

``is_running(pid_path) -> bool``
    External liveness probe: is *anyone* currently holding the kernel
    lock for this pidfile path? Reads kernel state, not file contents,
    so it cannot be fooled by a stale .pid file.

``write_pid(pid_path) -> None``
    Bare write of the .pid file. Kept for back-compat; new callers
    should prefer ``try_acquire_pidfile``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class _LockHandle:
    """Platform-specific lock handle. Subclasses implement ``release()``."""

    def release(self) -> None:
        raise NotImplementedError


# Per-process state: maps the resolved absolute pidfile path to the
# platform-specific lock handle. ``remove_pid`` consults this to release
# the right lock when the same process holds multiple (one for the agent,
# one for the Settings subprocess via gui/settings/lockfile.py — both
# delegate here).
_held_locks: "dict[str, _LockHandle]" = {}


def _lock_key(pid_path: Path) -> str:
    """Stable per-path key for ``_held_locks`` and the Windows mutex name."""
    try:
        return str(pid_path.resolve())
    except OSError:
        # Path's parent doesn't exist yet — fall back to the literal
        # absolute path. Safe because we only need a stable string.
        return str(pid_path.absolute())


# ============================================================
# Windows: named mutex
# ============================================================

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _ERROR_ALREADY_EXISTS = 183
    _ERROR_FILE_NOT_FOUND = 2
    _MUTEX_ALL_ACCESS = 0x1F0001

    def _mutex_name(pid_path: Path) -> str:
        """Derive a unique mutex name per pidfile path.

        ``Local\\`` namespace scopes the mutex to the current Windows
        login session — exactly the granularity we want for a per-user
        agent (two users on one machine each get their own primary). The
        path-derived suffix isolates independent installs and keeps the
        unit-test ``tmp_path`` tests from cross-contaminating.
        """
        digest = hashlib.sha1(_lock_key(pid_path).encode("utf-8")).hexdigest()[:16]
        return f"Local\\Sayzo-Lock-{digest}"

    class _WindowsMutex(_LockHandle):
        def __init__(self, handle: int) -> None:
            self._handle = handle

        def release(self) -> None:
            if self._handle:
                _kernel32.CloseHandle(self._handle)
                self._handle = 0

    def _try_acquire_kernel_lock(pid_path: Path) -> Optional[_LockHandle]:
        """``CreateMutexW`` with bInitialOwner=TRUE; treat ERROR_ALREADY_EXISTS as contention.

        ``CreateMutexW`` always returns a valid handle if the mutex
        could be created or opened. ``GetLastError() == ERROR_ALREADY_
        EXISTS`` means we opened an existing mutex — somebody else (or
        a stale handle from a crashed process, but the kernel reaps
        those on process death so this is impossible) holds it. We
        close our handle so we don't artificially extend the mutex's
        lifetime past the primary's death and return None.
        """
        name = _mutex_name(pid_path)
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, True, name)
        if not handle:
            err = ctypes.get_last_error()
            log.warning("[lock] CreateMutexW failed name=%s err=%d", name, err)
            return None
        err = ctypes.get_last_error()
        if err == _ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return None
        return _WindowsMutex(handle)

    def _is_kernel_lock_held(pid_path: Path) -> bool:
        """``OpenMutexW`` to probe existence without acquiring.

        Opens our own handle to the existing mutex (if any) and
        immediately closes it. Closing a non-owner handle just
        decrements the kernel reference count — it does NOT release
        ownership. The mutex stays alive while the primary holds its
        own handle, and the kernel destroys it the moment the primary
        dies (the only handle left).
        """
        name = _mutex_name(pid_path)
        ctypes.set_last_error(0)
        handle = _kernel32.OpenMutexW(_MUTEX_ALL_ACCESS, False, name)
        if not handle:
            err = ctypes.get_last_error()
            if err == _ERROR_FILE_NOT_FOUND:
                return False
            # ERROR_ACCESS_DENIED or other — mutex exists but we
            # can't touch it. Fail-closed (treat as held); double-
            # launching is worse than refusing to launch.
            return True
        _kernel32.CloseHandle(handle)
        return True


# ============================================================
# Unix (macOS / Linux): fcntl.flock
# ============================================================

else:
    import fcntl

    class _PosixFlock(_LockHandle):
        def __init__(self, fd: int) -> None:
            self._fd: Optional[int] = fd

        def release(self) -> None:
            if self._fd is None:
                return
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _try_acquire_kernel_lock(pid_path: Path) -> Optional[_LockHandle]:
        """``flock(fd, LOCK_EX | LOCK_NB)`` on the pidfile.

        flock is per-OFD; each ``os.open`` is an independent contender.
        The kernel auto-releases the lock when the fd closes — process
        exit (clean or otherwise) closes all fds, so a crashed primary
        never leaves a stale lock behind.
        """
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(pid_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            log.warning("[lock] open failed path=%s err=%s", pid_path, e)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
        return _PosixFlock(fd)

    def _is_kernel_lock_held(pid_path: Path) -> bool:
        """Probe by trying to flock then immediately unlocking.

        If we can grab LOCK_EX|LOCK_NB, nobody held it — we release
        right away and report False. If LOCK_NB raises BlockingIOError,
        somebody holds the flock — report True without disturbing them.
        """
        if not pid_path.exists():
            return False
        try:
            fd = os.open(str(pid_path), os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        return False


# ============================================================
# Public API
# ============================================================


def try_acquire_pidfile(pid_path: Path) -> bool:
    """Acquire the kernel single-instance lock and update the .pid file.

    Returns True if this process became the primary (kernel lock now
    owned, .pid file overwritten with our PID). Returns False if
    another live instance holds the lock.

    Non-reentrant: calling twice from the same process is undefined.
    Production callers acquire once at startup and call ``remove_pid``
    on exit.

    The kernel auto-releases the lock on process termination (clean
    exit, force kill, BSOD, reboot) — there is no userspace state that
    can be stale.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    handle = _try_acquire_kernel_lock(pid_path)
    if handle is None:
        return False

    _held_locks[_lock_key(pid_path)] = handle

    # Update the informational .pid file with our PID. A stale value
    # from a previous boot session gets overwritten here. Failure to
    # write is non-fatal: the kernel lock is what enforces single-
    # instance, the .pid file is just a sticky note for diagnostics +
    # IPC routing.
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        log.warning("[lock] failed to update pidfile %s (lock still held)", pid_path)
    return True


def remove_pid(pid_path: Path) -> None:
    """Release the kernel lock and remove the .pid file. Idempotent."""
    handle = _held_locks.pop(_lock_key(pid_path), None)
    if handle is not None:
        handle.release()
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        log.debug("[lock] failed to remove pidfile %s", pid_path, exc_info=True)


def is_running(pid_path: Path) -> bool:
    """True iff a Sayzo primary currently holds the kernel lock.

    Reads kernel state, not the .pid file contents — so it cannot be
    fooled by stale .pid files (recycled PID after reboot, leftover
    file from a crash, etc.).
    """
    return _is_kernel_lock_held(pid_path)


def write_pid(pid_path: Path) -> None:
    """Overwrite the .pid file with the current PID. No locking.

    Kept for back-compat with paths that already enforce single-
    instance some other way and just want the informational pidfile
    updated. New callers should prefer ``try_acquire_pidfile``.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
