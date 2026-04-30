"""macOS Dock icon visibility control via NSApp activation policy.

pywebview on macOS sets NSApp activation policy to ``Regular`` when a
window is created — this is needed so the window can take focus, but it
has the side-effect of adding a Dock icon to the process. For Sayzo
(an LSUIElement-bundled background app with a pystray status item) we
want the Dock icon hidden whenever no user-facing pywebview window is
actually visible:

* The agent process used pywebview during onboarding — once onboarding
  finishes, no pywebview window is visible, so the Dock icon should be
  hidden and only the pystray status item should remain.
* The Settings subprocess holds a *hidden* pywebview window in idle
  mode (pre-warming for instant open). It only needs the Dock icon
  while the window is actually shown.

This module exposes ``set_dock_visible(bool)`` which switches between
``Regular`` (Dock icon visible, can be foregrounded) and ``Accessory``
(no Dock icon, but pywebview windows can still be created and shown
via the usual show/activate path). Must be called on the main thread.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def set_dock_visible(visible: bool) -> bool:
    """Show or hide the Dock icon for the current process.

    No-op on non-darwin. Returns ``True`` on success, ``False`` on any
    error (missing pyobjc, exception in setActivationPolicy_, etc.).
    Safe to call repeatedly and from any code path that may also run
    on Windows / Linux — failures are swallowed and logged.
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSApp,
            NSApplicationActivationPolicyAccessory,
            NSApplicationActivationPolicyRegular,
        )
    except Exception:
        log.warning(
            "[mac_dock] AppKit unavailable — cannot adjust dock visibility",
            exc_info=True,
        )
        return False

    target = (
        NSApplicationActivationPolicyRegular
        if visible
        else NSApplicationActivationPolicyAccessory
    )
    try:
        NSApp.setActivationPolicy_(target)
        return True
    except Exception:
        log.warning("[mac_dock] setActivationPolicy_ failed", exc_info=True)
        return False


def activate_app() -> bool:
    """Bring the current process to the foreground.

    Wraps ``NSApp.activateIgnoringOtherApps_(True)``. Useful right
    after switching back to ``Regular`` activation policy, so the
    Settings window pops to focus instead of sitting behind whatever
    the user was looking at. No-op on non-darwin.
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApp  # type: ignore[import-not-found]
    except Exception:
        log.warning("[mac_dock] AppKit unavailable — cannot activate app", exc_info=True)
        return False
    try:
        NSApp.activateIgnoringOtherApps_(True)
        return True
    except Exception:
        log.warning("[mac_dock] activateIgnoringOtherApps_ failed", exc_info=True)
        return False
