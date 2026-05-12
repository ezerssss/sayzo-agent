"""Windows path for applying a staged update.

Spawns the staged NSIS installer with ``/S`` (silent) as a detached child
process and immediately exits the current agent. The NSIS installer's
existing kill-and-wait logic (see ``installer/windows/sayzo-agent.nsi`` lines
99-126) handles the brief window where the old agent is still tearing down.

The installer is responsible for:

  - Killing any leftover ``sayzo-agent.exe`` / ``sayzo-agent-service.exe``.
  - Replacing the bundle on disk.
  - Re-creating the Task Scheduler entry.
  - Relaunching the new ``sayzo-agent-service.exe service`` after install
    (the ``Section "Install"`` block triggers this when invoked silently — see
    plan's Stage 2 notes).

We never wait on the installer. The agent's process exits cleanly so the
installer's ``taskkill /F /T`` doesn't have to fight for it.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import NoReturn

from .update_stage import StagedUpdate

log = logging.getLogger(__name__)

# Win32 process-creation flags. Hardcoded so the module imports cleanly on
# non-Windows for testing and type-checking. ``subprocess.DETACHED_PROCESS``
# and ``subprocess.CREATE_NEW_PROCESS_GROUP`` exist only when stdlib detects
# the win32 platform at import time.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def spawn_installer_and_exit(staged: StagedUpdate) -> NoReturn:
    """Spawn the silent installer and call ``os._exit(0)``.

    Never returns. Any failure to spawn raises, which the caller must catch
    if they want to recover (e.g. fall back to "stay on current version").
    """
    payload = str(staged.payload_path)
    log.info(
        "[update-apply] spawning installer for v%s: %s /S",
        staged.version, payload,
    )

    # close_fds=True is the default on Windows since 3.7. DETACHED_PROCESS +
    # CREATE_NEW_PROCESS_GROUP gives the installer an entirely independent
    # console (it's silent so it has none anyway) and an independent
    # process group so a CTRL+BREAK to the agent's group can't propagate.
    # CREATE_NO_WINDOW belt-and-suspenders against an installer-internal
    # console flash.
    creationflags = (
        _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    )
    subprocess.Popen(
        [payload, "/S"],
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    log.info("[update-apply] installer spawned, exiting agent for swap")
    os._exit(0)
