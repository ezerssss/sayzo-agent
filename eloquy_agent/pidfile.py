"""PID file management to prevent duplicate service instances."""
from __future__ import annotations

import os
from pathlib import Path


def is_running(pid_path: Path) -> bool:
    """Check if a service is already running based on the PID file."""
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    # Check if the process is alive.
    try:
        os.kill(pid, 0)  # signal 0 = just check, don't kill
        return True
    except OSError:
        # Process doesn't exist — stale PID file.
        pid_path.unlink(missing_ok=True)
        return False


def write_pid(pid_path: Path) -> None:
    """Write the current process PID to the file."""
    pid_path.write_text(str(os.getpid()))


def remove_pid(pid_path: Path) -> None:
    """Remove the PID file."""
    pid_path.unlink(missing_ok=True)
