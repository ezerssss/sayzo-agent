"""Windows path for applying a staged update.

Spawns the staged NSIS installer with ``/S`` (silent) as a detached child
process and immediately exits the current agent.

In **silent** mode the installer deliberately does NOT ``taskkill`` the
running agent (it's a child of the dying agent in the kernel process tree,
so a ``/T`` tree-walk would kill the installer itself — see
``installer/windows/sayzo-agent.nsi`` v3.0.2 ``IfSilent`` guard). That makes
THIS process responsible for actually being gone — and gone cleanly — before
the installer's ``File /r`` starts overwriting the bundle. So before exiting
we: (1) sweep our own child processes (HUD subprocess + its QtWebEngine
helpers, the pre-warmed Settings subprocess) so they release their exe/DLL
handles, and (2) flush the log handlers so the last lines survive
``os._exit``. The installer's bounded delete-probe loop (nsi) is the
backstop if a handle is still held.

The installer then replaces the bundle, re-creates the autostart entry, and
relaunches the new ``sayzo-agent-service.exe service`` (the silent
``Section "Install"`` block does this). We never wait on the installer.
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


def _sweep_children(timeout_secs: float = 3.0) -> None:
    """Kill this process's child tree so exe/DLL handles are released.

    Direct ``kill()`` of our OWN children (by PID, recursively) — never an
    image-name tree-walk, so there's no way to hit the v3.0.0 self-kill
    class of bug. Best-effort: psutil missing or any error is swallowed.
    Runs BEFORE the installer is spawned, so the installer is not yet one of
    our children.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        log.debug("[update-apply] psutil unavailable — skipping child sweep")
        return
    try:
        kids = psutil.Process().children(recursive=True)
    except Exception:
        log.debug("[update-apply] could not enumerate children", exc_info=True)
        return
    if not kids:
        return
    try:
        gone, alive = psutil.wait_procs(kids, timeout=timeout_secs)
        for p in alive:
            try:
                log.warning(
                    "[update-apply] child pid=%s (%s) still alive — killing",
                    p.pid, p.name(),
                )
                p.kill()
            except Exception:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2.0)
    except Exception:
        log.warning("[update-apply] child sweep failed", exc_info=True)


def _flush_logging() -> None:
    """Flush root-logger handlers so the last lines survive ``os._exit``.

    Prefer flushing over ``logging.shutdown()`` — another thread logging a
    final line must not hit a closed handler.
    """
    try:
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
    except Exception:
        pass


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
    # Release our exe/DLL handles before the installer's File /r runs.
    _sweep_children()

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
    _flush_logging()
    os._exit(0)
