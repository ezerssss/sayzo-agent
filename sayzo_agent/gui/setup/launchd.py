"""macOS LaunchAgent registration via SMAppService.

Registers ``com.sayzo.agent`` as a Login Item so the service auto-starts at
login. No-op on non-darwin platforms. Idempotent — safe to call on every
launch.

Why SMAppService instead of writing to ``~/Library/LaunchAgents/`` directly:

macOS Ventura's Background Task Management subsystem fires a
"… added items that can run in the background" notification (and shows a
matching entry in System Settings -> Login Items) whenever a new background
item is registered. The text in that notification names the OWNER of the
item. macOS resolves owner in this order:

  1. The owning app bundle, when the agent is registered through
     SMAppService and its plist lives inside the bundle at
     ``Contents/Library/LaunchAgents/<label>.plist``.
  2. The Developer ID team identifier, as a fallback when the agent was
     registered by writing a free-standing plist into
     ``~/Library/LaunchAgents/`` (the pre-Ventura mechanism). macOS has no
     way to associate a free-standing plist with the .app that wrote it.

Sayzo is signed under the individual Developer ID "Sheen Santos Capadngan"
(team UYT2A4UX79). Under the legacy registration, users saw "Sheen Santos
Capadngan added items that can run in the background", which is alarming
and looks like malware. SMAppService.agent gives us "Sayzo" instead.

SMAppService is available on macOS 13+; Sayzo's floor is 14.4 (CoreAudio
Process Taps), so the API is always available where Sayzo runs.

Migration from v2.6.x and earlier: those releases wrote a free-standing
plist to ``~/Library/LaunchAgents/com.sayzo.agent.plist`` and let launchd
auto-load it at login. On the first launch of v2.7.0 (which is supervised
by that legacy registration, since launchd loaded it at this session's
login before we got here), this module:

  - Deletes the legacy plist file. The in-memory launchd registration for
    the current session is unaffected — the running process keeps running.
  - Calls SMAppService.register(). This adds the new registration to the
    BTM database, and fires the "Sayzo …" notification.
  - At the user's NEXT login, launchd finds no legacy plist on disk and
    loads only the SMAppService-registered one. From there on the BTM
    attribution is consistently "Sayzo".

We deliberately do NOT call ``launchctl bootout`` on the legacy job during
migration. We are the legacy job, and bootout would SIGTERM us mid-flow.
Letting the legacy registration die naturally at the next logout is the
clean path.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LAUNCH_AGENT_LABEL = "com.sayzo.agent"
LAUNCH_AGENT_PLIST_NAME = f"{LAUNCH_AGENT_LABEL}.plist"


def _legacy_plist_path() -> Path:
    """Path to the pre-v2.7.0 free-standing LaunchAgent plist."""
    return Path.home() / "Library" / "LaunchAgents" / LAUNCH_AGENT_PLIST_NAME


def _remove_legacy_plist() -> bool:
    """Delete ``~/Library/LaunchAgents/com.sayzo.agent.plist`` if it exists.

    Returns True if a file was removed (i.e. this is a v2.6.x -> v2.7.0
    upgrade); False if there was nothing to clean up.
    """
    path = _legacy_plist_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        log.info(
            "removed legacy launchd plist at %s — SMAppService now owns the "
            "registration",
            path,
        )
        return True
    except OSError:
        log.warning("could not remove legacy plist %s", path, exc_info=True)
        return False


def _register_via_smappservice() -> bool:
    """Call ``SMAppService.agentServiceWithPlistName_(...).register()``.

    The plist must be present in the running app bundle at
    ``Contents/Library/LaunchAgents/com.sayzo.agent.plist`` (sayzo-agent.spec
    drops it there during ``pyinstaller``). Returns True on success or if
    the agent is already registered. Logs and returns False on error.

    Outcomes (drawn from SMAppServiceStatus after register()):
      - Enabled: registered and allowed to run. Common path.
      - RequiresApproval: registered but the user has previously toggled
        Sayzo off in System Settings -> Login Items. Nothing we can do
        programmatically; the user has to re-enable it. Logged at WARNING.
      - NotRegistered / NotFound: registration didn't take. Logged at
        WARNING. Most often means the bundle plist is missing (build bug).
    """
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        log.warning(
            "pyobjc-framework-ServiceManagement not installed — cannot "
            "register Sayzo as a Login Item via SMAppService"
        )
        return False

    try:
        agent = SMAppService.agentServiceWithPlistName_(LAUNCH_AGENT_PLIST_NAME)
    except Exception:
        log.warning("SMAppService.agentServiceWithPlistName_ raised", exc_info=True)
        return False

    try:
        ok, err = agent.registerAndReturnError_(None)
    except Exception:
        log.warning("SMAppService.register raised", exc_info=True)
        return False

    if not ok:
        # err is an NSError (or None). description() is the canonical
        # human-readable message.
        msg = "<unknown>"
        if err is not None:
            try:
                msg = str(err.localizedDescription())
            except Exception:
                msg = repr(err)
        log.warning("SMAppService.register failed: %s", msg)
        return False

    # Status is informational only — register() already returned ok. We
    # log it so triage knows whether the user has the agent enabled.
    try:
        status = int(agent.status())
    except Exception:
        status = -1
    log.info(
        "SMAppService.register succeeded (status=%d for %s)",
        status,
        LAUNCH_AGENT_PLIST_NAME,
    )
    # SMAppServiceStatusRequiresApproval = 2: registered, but disabled by
    # the user in System Settings. Surface this so the diagnose-notifications
    # path (or future Settings UI) can prompt them.
    if status == 2:
        log.warning(
            "Sayzo Login Item is registered but disabled in System Settings "
            "-> Login Items. The user must re-enable it for auto-start."
        )
    return True


def ensure_launchd_registered(*, load_immediately: bool = False) -> bool:
    """Register Sayzo to auto-start on login via SMAppService.

    Idempotent. Safe to call on every successful first-run completion and
    on every service start.

    The ``load_immediately`` argument is preserved for API compatibility
    with the pre-v2.7.0 signature but is now a no-op: SMAppService.register
    integrates with launchd directly, so there is no separate "load" step.
    The currently running process — which on a v2.6.x -> v2.7.0 upgrade is
    still being supervised by the LEGACY launchd registration — keeps
    running until natural exit, and the new SMAppService registration takes
    over at the user's next login.

    Returns:
        True  if the registration call succeeded (or was already in place)
        False on non-darwin, missing pyobjc, missing bundle, or API failure

    Never raises.
    """
    del load_immediately  # accepted for backward compat; see docstring

    if sys.platform != "darwin":
        return False

    # In a frozen .app bundle, ``sys.executable`` is
    # /Applications/Sayzo.app/Contents/MacOS/sayzo-agent. Walk back up to
    # confirm the bundle plist is in place. Skip the call (and the legacy
    # cleanup) when we can't find the bundle — that's a dev / source run
    # where there's no .app to register, and any legacy plist on disk is
    # presumably from a prior installer test that the developer can clean
    # up themselves.
    exe_path = Path(sys.executable)
    bundle_root: Path | None = None
    for parent in exe_path.parents:
        if parent.suffix == ".app":
            bundle_root = parent
            break
    if bundle_root is None:
        log.info(
            "skipping SMAppService registration: not running from a .app bundle "
            "(exe=%s) — likely a dev run",
            exe_path,
        )
        return False

    bundle_plist = (
        bundle_root / "Contents" / "Library" / "LaunchAgents" / LAUNCH_AGENT_PLIST_NAME
    )
    if not bundle_plist.exists():
        log.warning(
            "skipping SMAppService registration: bundle plist missing at %s "
            "(build bug — check sayzo-agent.spec post-BUNDLE step)",
            bundle_plist,
        )
        return False

    # Migration: drop the legacy free-standing plist, if any. Logged at
    # INFO when something was actually removed so triage can confirm
    # upgrades took effect.
    _remove_legacy_plist()

    return _register_via_smappservice()


def is_registered() -> bool:
    """Return True if SMAppService reports the agent as Enabled.

    Used by ``sayzo-agent service`` to decide whether to skip its own
    fallback Popen of the service binary — when launchd is going to start
    Sayzo for us, we should not race it. False covers every non-Enabled
    state (NotRegistered / RequiresApproval / NotFound) plus non-darwin and
    missing pyobjc, since none of those guarantee a launchd-driven start.
    """
    if sys.platform != "darwin":
        return False
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        agent = SMAppService.agentServiceWithPlistName_(LAUNCH_AGENT_PLIST_NAME)
        return int(agent.status()) == 1  # SMAppServiceStatusEnabled
    except Exception:
        return False


def unregister_login_item() -> bool:
    """Permanently unregister the Sayzo Login Item.

    Not currently wired into any user-facing flow — exposed for a possible
    "Disable auto-start" toggle in Settings, and for clean uninstall.
    Tray "Quit" must NOT call this: quitting from the menu bar is intended
    to stop the agent for the current session only, with auto-start still
    in effect for the next login. Returns True on success.
    """
    if sys.platform != "darwin":
        return False
    try:
        from ServiceManagement import SMAppService  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        agent = SMAppService.agentServiceWithPlistName_(LAUNCH_AGENT_PLIST_NAME)
        ok, err = agent.unregisterAndReturnError_(None)
    except Exception:
        log.warning("SMAppService.unregister raised", exc_info=True)
        return False
    if not ok:
        msg = "<unknown>"
        if err is not None:
            try:
                msg = str(err.localizedDescription())
            except Exception:
                msg = repr(err)
        log.warning("SMAppService.unregister failed: %s", msg)
        return False
    return True


__all__ = [
    "LAUNCH_AGENT_LABEL",
    "LAUNCH_AGENT_PLIST_NAME",
    "ensure_launchd_registered",
    "is_registered",
    "unregister_login_item",
]
