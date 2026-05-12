"""Windows-specific first-run permission helpers (HUD-era stubs).

Pre-v2.10 this module owned the Notifications onboarding screen's
permission flow on Windows — reading WinRT toast authorisation,
opening ``ms-settings:notifications``, sending a verification toast.
With the v2.10 HUD rewrite (`project_custom_hud_shipped`),
notifications no longer go through the OS notification surface, so
the screen was removed and the underlying functions don't need to do
anything anymore. The bridge methods that bound to them still exist
(legacy bindings) so the JS API surface didn't change; these
functions return constants indicating "already granted / nothing to
do" so any straggler caller doesn't block setup.

Mic and WASAPI loopback don't surface a blocking OS dialog the way
macOS TCC does — if the user has mic privacy disabled in Windows
Settings, capture fails at runtime with a PortAudio error (handled
upstream). That path remains out of scope here.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)


def has_notification_permission() -> Optional[bool]:
    """No-op stub — see module docstring.

    Pre-v2.10 this returned the WinRT toast authorisation status. With
    the HUD owning the notification surface, there's no OS permission
    to check. Returns ``True`` so legacy callers see "all good".
    """
    if sys.platform != "win32":
        return None
    return True


def open_notification_settings() -> None:
    """Open Settings → System → Notifications via the ms-settings URI.

    Kept as a real call: a user reading the Settings → Notifications
    pane in Sayzo (if any UI surface still surfaces this link) gets
    deep-linked to the relevant OS panel. The OS-side toggle no longer
    affects whether Sayzo notifications appear — the HUD bypasses the
    notification subsystem entirely — but opening the panel is harmless.
    """
    if sys.platform != "win32":
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "ms-settings:notifications"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        log.warning("[win_permissions] open settings failed: %s", e)


def send_verification_notification() -> bool:
    """No-op stub — see module docstring.

    Pre-v2.10 this fired a real WinRT toast as a ground-truth check
    that the user could see notifications. The HUD doesn't have an
    OS-side suppression layer to verify against, so the function
    short-circuits to False (didn't send anything).
    """
    return False
