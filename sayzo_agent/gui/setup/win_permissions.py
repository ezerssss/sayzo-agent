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
            from desktop_notifier import Icon
            from desktop_notifier.sync import DesktopNotifierSync

            from sayzo_agent.gui.common.assets import notification_icon_path

            try:
                logging.getLogger("desktop_notifier").setLevel(logging.INFO)
            except Exception:
                pass

            # Pass our own icon so the WinRT backend's register_hkey writes
            # ``HKCU\SOFTWARE\Classes\AppUserModelId\Sayzo\IconUri`` pointing
            # at our logo. Without this, ``app_icon`` defaults to the
            # bundled ``desktop_notifier/resources/python.png`` and the
            # Notifications onboarding test toast shows a Python snake
            # icon — which is what users see, and is exactly the wrong
            # first impression. Same fix applies on macOS.
            icon_p = notification_icon_path()
            app_icon = Icon(path=icon_p) if icon_p else None
            log.info(
                "[win_permissions] notifier init: icon=%s exists=%s",
                icon_p,
                icon_p.exists() if icon_p else False,
            )
            _NOTIFIER = DesktopNotifierSync(app_name="Sayzo", app_icon=app_icon)
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


def send_verification_notification() -> bool:
    """Fire a single test toast so the user can confirm notifications
    actually appear on their screen. ``has_authorisation()`` returning True
    isn't always enough — Focus Assist + the per-app toggle can still
    swallow toasts silently. An actual toast hitting the screen is the
    ground-truth check.

    Returns True on best-effort send success, False otherwise. Failures
    are logged but never propagated.
    """
    if sys.platform != "win32":
        return False
    notifier = _get_notifier()
    if notifier is None:
        log.warning(
            "[win_permissions] verification toast skipped — notifier unavailable"
        )
        return False
    log.info("[win_permissions] verification toast: send begin")
    try:
        identifier = notifier.send(
            title="Sayzo notifications are on",
            message="You'll see prompts like this when Sayzo spots a meeting.",
        )
        log.info(
            "[win_permissions] verification toast: send done id=%s", identifier
        )
        return True
    except Exception:
        log.warning(
            "[win_permissions] send_verification_notification failed",
            exc_info=True,
        )
        return False
