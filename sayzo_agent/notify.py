"""Cross-platform desktop notifications for conversation events.

Failures are always swallowed and logged — a broken toast backend must never
bring down the main capture pipeline.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...


class NoopNotifier:
    def notify(self, title: str, body: str) -> None:
        log.debug("[notify] (noop) %s — %s", title, body)


def _logo_path() -> Path:
    """Resolve the Sayzo logo bundled alongside the tray icon.

    Mirrors ``sayzo_agent/gui/tray.py::_logo_path`` so dev and PyInstaller-
    frozen builds both land on the same asset.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    else:
        # notify.py is sayzo_agent/notify.py — parent.parent is repo root.
        base = Path(__file__).resolve().parent.parent / "installer" / "assets"
    return base / "logo.png"


class DesktopNotifier:
    """Native toast via the `desktop-notifier` PyPI package.

    Uses the library's synchronous wrapper (``DesktopNotifierSync``) which
    keeps one persistent asyncio loop alive inside the object. That matters on
    the Windows WinRT backend: it registers internal handlers that marshal
    back into the loop via ``call_soon_threadsafe`` when a toast is
    activated / dismissed / fails. Spinning up ``asyncio.run()`` per call
    (our earlier approach) closed the loop on return, so WinRT's later
    callback landed on a dead loop and logged
    ``RuntimeError: Event loop is closed``. With a persistent loop the
    callback just queues and never fires — harmless here since we don't
    register interaction handlers.

    Backend construction is lazy. On Windows the WinRT backend marshals COM
    interfaces to the thread that built it; any cross-thread ``.send()`` from
    another thread raises ``RPC_E_WRONG_THREAD`` (WinError -2147417842). The
    sink dispatches every ``notify()`` call on the heavy-worker pool
    (``ThreadPoolExecutor(max_workers=1)``), so building the backend on first
    call pins the COM apartment to that worker for the life of the process.

    ``app_name`` must match the AUMID set on the Start Menu shortcut by the
    NSIS installer ("Sayzo.Agent") for WinRT toasts to appear at all on
    Windows 10; on macOS it's the display name attributed to the notification.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._app_name = app_name
        self._impl = None
        self._init_failed = False
        self._init_lock = threading.Lock()

        # On Windows the desktop-notifier backend activates winrt notification
        # APIs, which load Windows Runtime DLLs that subsequently break torch's
        # own DLL initialization (c10.dll) — any later `import torch` via
        # silero_vad dies with WinError 1114. Preloading torch first pins its
        # DLLs so the winrt load can't clobber them. The PyInstaller bundle
        # sidesteps this by shipping DLLs next to the exe; dev installs don't.
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
                from desktop_notifier import Icon
                from desktop_notifier.sync import DesktopNotifierSync

                icon_path = _logo_path()
                app_icon = Icon(path=icon_path) if icon_path.exists() else None
                self._impl = DesktopNotifierSync(
                    app_name=self._app_name, app_icon=app_icon
                )
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
            self._impl.send(title=title, message=body)
        except Exception:
            log.warning("[notify] send failed", exc_info=True)
