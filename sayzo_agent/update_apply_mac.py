"""macOS path for applying a staged update.

Spawns ``apply_update.sh`` (bundled at ``Contents/Resources/apply_update.sh``)
as a detached child process that survives the agent's exit, then immediately
exits. The helper waits for the agent's PID lock to release, mounts the
staged DMG, ``rsync``s the new .app over the live bundle, unmounts, and
relaunches via ``open --args service``.

We rely on detachment via ``start_new_session=True`` (POSIX setsid) so the
helper inherits PID 1 (launchd) as its parent the moment the agent process
exits. No nohup wrapper required.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional

from .update_stage import StagedUpdate

log = logging.getLogger(__name__)

HELPER_NAME = "apply_update.sh"


def _locate_helper() -> Optional[Path]:
    """Find ``apply_update.sh``.

    Resolution order:
      1. Frozen bundle: ``<.app>/Contents/Resources/apply_update.sh``. The
         spec adds the script to PyInstaller's ``datas`` so it lands here.
      2. Dev source tree: ``<repo>/installer/macos/apply_update.sh``. Lets a
         developer running ``python -m sayzo_agent service`` exercise the
         apply path without a full rebuild.

    Returns ``None`` if neither is found (the caller logs and bails out —
    the user's agent stays on the current version, which is the safe call).
    """
    # Frozen-bundle path. sys.executable in a PyInstaller .app is
    # <bundle>/Contents/MacOS/<name>, so the helper is two parents up under
    # Contents/Resources/.
    exe = Path(sys.executable).resolve()
    bundled = exe.parent.parent / "Resources" / HELPER_NAME
    if bundled.is_file():
        return bundled

    # Dev path. __file__ is sayzo_agent/update_apply_mac.py.
    dev = Path(__file__).resolve().parent.parent / "installer" / "macos" / HELPER_NAME
    if dev.is_file():
        return dev

    return None


def _locate_app_bundle() -> Optional[Path]:
    """Return the path to the running ``Sayzo.app`` bundle, or None in dev.

    In a frozen bundle, ``sys.executable`` is
    ``<somewhere>/Sayzo.app/Contents/MacOS/sayzo-agent``. The .app dir is
    three parents up. In a dev source tree there is no .app and the apply
    path is unsupported (we don't want to rsync the user's /Applications
    when they're running the agent from source).
    """
    exe = Path(sys.executable).resolve()
    candidate = exe.parent.parent.parent
    if candidate.suffix == ".app" and candidate.is_dir():
        return candidate
    return None


def spawn_swap_helper_and_exit(staged: StagedUpdate) -> NoReturn:
    """Spawn ``apply_update.sh`` detached, then ``os._exit(0)``.

    Never returns. Raises if the helper or the .app bundle can't be located —
    callers in the agent's quit path should catch and fall through to normal
    exit so the user is at least not stuck.
    """
    helper = _locate_helper()
    if helper is None:
        raise RuntimeError(
            f"can't locate {HELPER_NAME} for macOS update apply"
        )
    app_bundle = _locate_app_bundle()
    if app_bundle is None:
        raise RuntimeError(
            "can't locate Sayzo.app bundle (running from source?) — "
            "apply path unsupported in dev"
        )

    payload = str(staged.payload_path)
    log.info(
        "[update-apply] spawning swap helper for v%s: %s %s %s",
        staged.version, helper, payload, app_bundle,
    )

    subprocess.Popen(
        ["/bin/bash", str(helper), payload, str(app_bundle)],
        start_new_session=True,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    log.info("[update-apply] swap helper spawned, exiting agent for swap")
    os._exit(0)
