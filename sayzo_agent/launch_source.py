"""Heuristic to distinguish a user-clicked launch from an auto-start.

Used by ``service()`` to decide whether to auto-surface the Settings window
when the agent starts and no primary was running. Two requirements that
determine the heuristic:

* **User clicks the Sayzo icon (Start Menu, desktop, .exe double-click,
  Spotlight, Finder, Dock-after-not-running)** → open Settings so the
  user gets visual confirmation Sayzo is alive.
* **OS-level auto-start (Windows Task Scheduler at login, macOS launchd
  LaunchAgent)** → run silently in the background.

The two paths invoke the same ``service`` subcommand with the same args,
so we discriminate via parent-process / environment inspection. Returns
``False`` on any detection failure — that's the safe default (treat as
auto-start, run silently) so a heuristic miss never accidentally pops
Settings on every login.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)


# Windows process names that signify the user opened us via the OS shell.
# explorer.exe hosts the Start Menu, desktop, taskbar, Run dialog, and is
# the parent for double-clicked .exe / .lnk files. Anything else
# (cmd.exe, pwsh.exe, taskeng.exe, svchost.exe, services.exe,
# sayzo-setup.exe, etc.) is treated as auto-start / dev-launch and stays
# silent. Conservative on purpose — false positives auto-open Settings
# at every boot, which is much worse than a false negative.
_WIN_USER_SHELL_NAMES = frozenset({"explorer.exe"})

# macOS bundle identifier from sayzo-agent.spec — the discriminator for
# "is this process running inside the Sayzo.app bundle?" Cocoa sets the
# ``__CFBundleIdentifier`` env var to this value for any bundle-launched
# process (LaunchServices spawn, launchd-spawn, or even direct invocation
# of the bundle's executable from Terminal).
_MAC_BUNDLE_ID = "com.sayzo.agent"


def looks_user_launched() -> bool:
    """Return True iff this process looks like the user clicked the icon.

    Exposed as a function (not a constant) so the lookup happens at the
    call site, after fork/spawn semantics have settled. Cheap (~1 ms) —
    safe to call inline.
    """
    if sys.platform == "win32":
        return _looks_user_launched_win()
    if sys.platform == "darwin":
        return _looks_user_launched_mac()
    return False


def _looks_user_launched_win() -> bool:
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:
        log.debug("[launch_source] psutil unavailable", exc_info=True)
        return False

    try:
        parent = psutil.Process(os.getpid()).parent()
        if parent is None:
            return False
        name = parent.name().lower()
    except Exception:
        log.debug("[launch_source] parent lookup failed", exc_info=True)
        return False

    log.info("[launch_source] parent process name=%s", name)
    return name in _WIN_USER_SHELL_NAMES


def _looks_user_launched_mac() -> bool:
    """Inspect launchd / LaunchServices env-var fingerprints.

    The three cases that matter:

    1. ``__CFBundleIdentifier`` matches our bundle AND ``XPC_SERVICE_NAME``
       is not a com.sayzo.* label → LaunchServices spawn (Finder /
       Spotlight / Dock-while-not-running double-click). User-launched.
    2. ``__CFBundleIdentifier`` matches AND ``XPC_SERVICE_NAME`` is a
       com.sayzo.* label → launchd LaunchAgent at login. Auto-start.
    3. ``__CFBundleIdentifier`` doesn't match (e.g. dev source: ``python
       -m sayzo_agent service`` from Terminal) → not running from the
       bundle. Treated as silent so dev invocations don't auto-pop
       Settings every time the developer iterates.

    The fingerprints aren't documented Apple API but have been stable
    across macOS releases for >10 years (XPC_SERVICE_NAME is set by
    ``launchd``; ``__CFBundleIdentifier`` is set by ``CoreFoundation``
    when the executable is found inside a .app bundle).
    """
    bundle_id = os.environ.get("__CFBundleIdentifier", "")
    xpc = os.environ.get("XPC_SERVICE_NAME", "")
    log.info(
        "[launch_source] __CFBundleIdentifier=%r XPC_SERVICE_NAME=%r",
        bundle_id, xpc,
    )

    if bundle_id != _MAC_BUNDLE_ID:
        return False
    if xpc.startswith("com.sayzo."):
        return False
    return True
