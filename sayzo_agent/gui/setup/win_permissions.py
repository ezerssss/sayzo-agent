"""Windows-specific first-run permission helpers.

Only notifications require explicit attention. Mic and WASAPI loopback
don't surface a blocking OS dialog the way macOS TCC does — if the user
has mic privacy disabled in Windows Settings, capture fails at runtime
with a PortAudio error (handled upstream). That path is out of scope here.

For notifications, Windows doesn't expose a programmatic "request
permission" flow: toasts either appear (AUMID registered + not blocked
by Focus Assist or per-app setting) or they don't. All we can do is read
the current status and point the user at ``ms-settings:notifications``
if they're blocked.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger(__name__)

_NOTIFIER = None
_NOTIFIER_INIT_FAILED = False
_NOTIFIER_LOCK = threading.Lock()


def _get_notifier():
    global _NOTIFIER, _NOTIFIER_INIT_FAILED
    if _NOTIFIER is not None or _NOTIFIER_INIT_FAILED:
        return _NOTIFIER
    with _NOTIFIER_LOCK:
        if _NOTIFIER is not None or _NOTIFIER_INIT_FAILED:
            return _NOTIFIER
        # Preload torch before the WinRT backend loads winsdk DLLs — otherwise
        # c10.dll init later fails (see notify.py for the original rationale).
        if sys.platform == "win32":
            try:
                import torch  # noqa: F401
            except Exception:
                pass
        try:
            from desktop_notifier.sync import DesktopNotifierSync

            _NOTIFIER = DesktopNotifierSync(app_name="Sayzo")
        except Exception:
            _NOTIFIER_INIT_FAILED = True
            log.warning(
                "[win_permissions] DesktopNotifierSync init failed",
                exc_info=True,
            )
    return _NOTIFIER


def has_notification_permission() -> Optional[bool]:
    """Return True if Windows will currently display Sayzo toasts (AUMID
    registered, not blocked by Focus Assist or per-app toggle), False if
    blocked, None on error."""
    if sys.platform != "win32":
        return None
    notifier = _get_notifier()
    if notifier is None:
        return None
    try:
        return bool(notifier.has_authorisation())
    except Exception:
        log.warning(
            "[win_permissions] has_authorisation failed", exc_info=True
        )
        return None


def open_notification_settings() -> None:
    """Open Settings → System → Notifications via the ms-settings URI."""
    if sys.platform != "win32":
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "ms-settings:notifications"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        log.warning("[win_permissions] open settings failed: %s", e)
