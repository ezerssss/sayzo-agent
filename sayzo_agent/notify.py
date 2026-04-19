"""Cross-platform desktop notifications for conversation events.

Failures are always swallowed and logged — a broken toast backend must never
bring down the main capture pipeline.
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...


class NoopNotifier:
    def notify(self, title: str, body: str) -> None:
        log.debug("[notify] (noop) %s — %s", title, body)


class DesktopNotifier:
    """Native toast via the `desktop-notifier` PyPI package.

    `app_name` is used on Windows to match the AUMID on the Start Menu shortcut
    (set in the NSIS installer); on macOS it's the display name the system
    attributes the notification to.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._impl = None
        try:
            from desktop_notifier import DesktopNotifier as _Backend
            self._impl = _Backend(app_name=app_name)
        except Exception:
            log.warning("[notify] backend init failed; toasts disabled", exc_info=True)

    def notify(self, title: str, body: str) -> None:
        if self._impl is None:
            return
        try:
            self._impl.send_sync(title=title, message=body)
        except Exception:
            log.warning("[notify] send failed", exc_info=True)
