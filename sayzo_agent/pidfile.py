"""PID file management to prevent duplicate service instances.

The contract used by ``service()`` / ``run()``:

1. Call :func:`try_acquire_pidfile` — atomic create-or-fail. Returns True
   if we became the primary; False if another live instance beat us.
2. On success, the agent runs and the pidfile lives until step 3.
3. Call :func:`remove_pid` in a ``finally`` block on exit.

The earlier two-step pattern (``is_running`` then ``write_pid``) had a
TOCTOU window: two concurrent launches could both observe "no pidfile"
and then both write it, ending up with two live primaries. Atomic
``O_CREAT | O_EXCL`` closes that window — only one process can win the
exclusive create.
"""
from __future__ import annotations

import os
from pathlib import Path


def is_running(pid_path: Path) -> bool:
    """Check if a service is already running based on the PID file.

    Returns False on missing file or stale PID; True if the recorded PID
    is alive.

    Cross-privilege correctness on Windows is the load-bearing detail
    here. The naive ``os.kill(pid, 0)`` idiom calls
    ``OpenProcess(PROCESS_ALL_ACCESS, …)`` under the hood, which fails
    with ``ERROR_ACCESS_DENIED`` (→ Python ``PermissionError``) when the
    target process belongs to a higher integrity level — e.g., the
    post-install Sayzo agent inherits NSIS's elevated token, and a
    later user-clicked Sayzo (medium integrity) can't open it. The old
    code treated ``PermissionError`` as "process dead", removed the
    pidfile, and let the secondary become a second primary. ``psutil
    .pid_exists`` queries the OS without needing PROCESS_ALL_ACCESS, so
    it returns True correctly in that case.

    Stale pidfiles are removed so a crashed primary doesn't
    permanently lock subsequent launches out.
    """
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False

    try:
        import psutil  # type: ignore[import-not-found]

        if psutil.pid_exists(pid):
            return True
        # Confirmed gone — clean up the stale file.
        pid_path.unlink(missing_ok=True)
        return False
    except Exception:
        # psutil missing / OS query failed — fall through to the
        # ``os.kill`` path with explicit cross-privilege handling.
        pass

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process is alive but in a higher integrity level (or another
        # user) we can't query. Don't touch the pidfile.
        return True
    except OSError:
        pid_path.unlink(missing_ok=True)
        return False


def try_acquire_pidfile(pid_path: Path) -> bool:
    """Atomically claim the pidfile or report that another instance has it.

    Returns True if this process became the primary (pidfile now contains
    our PID); False if another live instance beat us.

    Implementation: ``O_CREAT | O_EXCL`` exclusive create races atomically
    at the OS level. If it fails because the file exists, we re-check
    ``is_running`` — a stale pidfile (process dead) is removed and we
    retry the exclusive create exactly once. Two retries would loop on
    a true race; bounding it at one keeps the worst case to "lose to a
    same-instant competitor", which is the right behavior — if we lost
    the race, we should exit, not keep trying.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    payload = str(os.getpid()).encode()

    for attempt in range(2):
        try:
            fd = os.open(
                pid_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            # Someone else holds it. If they're alive, we lose.
            if is_running(pid_path):
                return False
            # Holder is dead OR the pidfile is corrupt (non-numeric, empty,
            # zero PID). ``is_running`` only auto-removes the dead-PID case;
            # we clean up the rest ourselves so the retry's O_EXCL can win.
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                return False
            continue
        except OSError:
            # Permissions / disk full / unusual fs. Treat as "couldn't
            # acquire" rather than crashing — the caller will exit.
            return False

        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    # Lost the race even after retry — give up.
    return False


def write_pid(pid_path: Path) -> None:
    """Overwrite the PID file with the current PID. Non-atomic.

    Kept for backward compatibility with paths that already enforce
    single-instance some other way. New callers should prefer
    :func:`try_acquire_pidfile`.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def remove_pid(pid_path: Path) -> None:
    """Remove the PID file."""
    pid_path.unlink(missing_ok=True)
