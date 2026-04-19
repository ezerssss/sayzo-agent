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
        # Defer backend construction until the first notify() call. On
        # Windows, the desktop-notifier WinRT backend marshals its COM
        # interface for the thread that created it; any cross-thread
        # `.show()` fails with RPC_E_WRONG_THREAD (WinError -2147417842).
        # Our executor pins all notify() calls to one worker thread
        # (ThreadPoolExecutor max_workers=1), so building the backend on
        # first call binds the COM apartment to that thread permanently.
        self._app_name = app_name
        self._impl = None
        self._init_failed = False
        self._init_lock = __import__("threading").Lock()

        # On Windows the desktop-notifier backend activates winrt notification
        # APIs, which load Windows Runtime DLLs that subsequently break torch's
        # own DLL initialization (c10.dll) — any later `import torch` via
        # silero_vad dies with WinError 1114. Preloading torch first pins its
        # DLLs so the winrt load can't clobber them. The PyInstaller bundle
        # sidesteps this by shipping DLLs next to the exe; dev installs don't.
        import sys
        if sys.platform == "win32":
            try:
                import torch  # noqa: F401
            except Exception:
                pass

    def _ensure_impl(self) -> None:
        """Construct the backend lazily on whichever thread first calls
        notify(). Subsequent calls reuse the cached instance."""
        if self._impl is not None or self._init_failed:
            return
        with self._init_lock:
            if self._impl is not None or self._init_failed:
                return
            try:
                from desktop_notifier import DesktopNotifier as _Backend
                self._impl = _Backend(app_name=self._app_name)
            except Exception:
                self._init_failed = True
                log.warning(
                    "[notify] backend init failed; toasts disabled", exc_info=True
                )

    def notify(self, title: str, body: str) -> None:
        self._ensure_impl()
        if self._impl is None:
            return
        try:
            asyncio.run(self._impl.send(title=title, message=body))
        except Exception:
            log.warning("[notify] send failed", exc_info=True)
