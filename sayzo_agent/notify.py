"""Cross-platform desktop notifications for conversation events.

Failures are always swallowed and logged — a broken toast backend must never
bring down the main capture pipeline.
"""
from __future__ import annotations

import asyncio
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

    ``app_name`` is used on Windows to match the AUMID on the Start Menu
    shortcut (set in the NSIS installer); on macOS it's the display name
    attributed to the notification.

    ``notify()`` is synchronous for compatibility with sink.py's executor-
    dispatched call site; we wrap the now-async backend via ``asyncio.run``
    which creates a short-lived loop per call. Expected to be invoked from
    the heavy-worker thread pool, not from the main asyncio loop — if it
    ever ends up called from inside a running loop we'll hit a RuntimeError
    and just log it.
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
            asyncio.run(self._impl.send(title=title, message=body))
        except Exception:
            log.warning("[notify] send failed", exc_info=True)
