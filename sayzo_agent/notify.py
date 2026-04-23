"""Cross-platform desktop notifications for conversation events.

Failures are always swallowed and logged — a broken toast backend must never
bring down the main capture pipeline.

In the armed-only model (v1.0+) this module has two jobs:

1. **Fire-and-forget** via ``notify(title, body)`` — same semantics as
   before (capture-saved, post-arm guidance, stream-open error, etc.).
2. **Interactive consent** via ``ask_consent(...)`` — toast with two action
   buttons, await the user's click (or timeout), return ``"yes"``, ``"no"``,
   or ``"timeout"``. Used by the ArmController for whitelist consent, hotkey
   start/stop confirmation, end-of-meeting confirmation, long-meeting
   check-in, and meeting-ended-watcher toasts.

Interactive buttons require ``desktop-notifier``'s async API. The sync
wrapper we used to rely on is deprecated and doesn't round-trip button
callbacks reliably on Windows. Instead we spin up a dedicated asyncio loop
on a background daemon thread (created eagerly in ``__init__`` so the first
consent toast isn't gated on loop startup), and marshal all work onto it
via ``asyncio.run_coroutine_threadsafe``.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Literal, Optional, Protocol

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


# AUMID that every Sayzo notifier instance must be constructed with. Must
# match the Start Menu shortcut AppUserModelID set by the NSIS installer
# (see ``installer/windows/sayzo-agent.nsi``) — otherwise WinRT toasts
# silently fail to render on Windows 10. On macOS this is the display name
# attributed to the notification. Any drift is a silent regression.
APP_AUMID = "Sayzo.Agent"


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...


class NoopNotifier:
    def notify(self, title: str, body: str) -> None:
        log.debug("[notify] (noop) %s — %s", title, body)

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "timeout",
    ) -> ConsentResult:
        log.debug("[notify] (noop) consent %s — %s → %s", title, body, default_on_timeout)
        return default_on_timeout


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
    """Native toast via the `desktop-notifier` PyPI package, async backend.

    Owns a dedicated asyncio loop running on a daemon background thread.
    Both ``notify`` and ``ask_consent`` are thread-safe — they marshal onto
    the loop via ``asyncio.run_coroutine_threadsafe``.

    The backend is constructed eagerly in ``__init__`` (on the background
    thread, since Windows WinRT pins COM to the constructing thread). If
    backend init fails the notifier degrades to a noop — exceptions are
    logged, never propagated.

    ``app_name`` must match the AUMID set on the Start Menu shortcut by the
    NSIS installer ("Sayzo.Agent") for WinRT toasts to appear at all on
    Windows 10; on macOS it's the display name attributed to the notification.
    Interactive button callbacks work on both WinRT (Windows 10+) and
    NSUserNotification (macOS) back-ends.
    """

    def __init__(self, app_name: str = "Sayzo") -> None:
        self._app_name = app_name
        self._impl = None  # desktop_notifier.DesktopNotifier instance
        self._init_failed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()

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

        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{app_name}-notifier",
            daemon=True,
        )
        self._thread.start()
        # Block briefly for the loop to come up so the first caller doesn't
        # race the loop creation. If the thread errors out before ready, the
        # wait times out and later calls no-op.
        self._loop_ready.wait(timeout=5.0)

    # ---- loop thread -------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except Exception:
            self._init_failed = True
            self._loop_ready.set()
            log.warning("[notify] event loop init failed; toasts disabled", exc_info=True)
            return

        try:
            from desktop_notifier import DesktopNotifier as _Async, Icon
            icon_path = _logo_path()
            app_icon = Icon(path=icon_path) if icon_path.exists() else None
            self._impl = _Async(app_name=self._app_name, app_icon=app_icon)
        except Exception:
            self._init_failed = True
            log.warning(
                "[notify] backend init failed; toasts disabled", exc_info=True
            )

        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    # ---- public API --------------------------------------------------------

    def notify(self, title: str, body: str) -> None:
        """Fire-and-forget toast. Thread-safe."""
        if self._init_failed or self._impl is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._send(title, body), self._loop
            )
        except Exception:
            log.warning("[notify] schedule failed", exc_info=True)

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult:
        """Show an interactive toast with two action buttons, block up to
        ``timeout_secs`` for a response, return ``"yes"``, ``"no"``, or
        ``"timeout"`` (which maps to ``default_on_timeout`` in the caller's
        semantic layer if desired — we pass it through distinctly so callers
        can distinguish "clicked No" from "ignored")."""
        if self._init_failed or self._impl is None or self._loop is None:
            return default_on_timeout
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._ask(title, body, yes_label, no_label, timeout_secs),
                self._loop,
            )
            return fut.result(timeout=timeout_secs + 5.0)
        except Exception:
            log.warning("[notify] ask_consent failed", exc_info=True)
            return default_on_timeout

    # ---- loop-local coroutines ---------------------------------------------

    async def _send(self, title: str, body: str) -> None:
        try:
            assert self._impl is not None
            await self._impl.send(title=title, message=body)
        except Exception:
            log.warning("[notify] send failed", exc_info=True)

    async def _ask(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
    ) -> ConsentResult:
        from desktop_notifier import Button

        loop = asyncio.get_running_loop()
        result_fut: asyncio.Future[ConsentResult] = loop.create_future()

        def _resolve(value: ConsentResult) -> None:
            if not result_fut.done():
                result_fut.set_result(value)

        yes_btn = Button(title=yes_label, on_pressed=lambda: _resolve("yes"))
        no_btn = Button(title=no_label, on_pressed=lambda: _resolve("no"))

        try:
            assert self._impl is not None
            await self._impl.send(
                title=title, message=body, buttons=[yes_btn, no_btn]
            )
        except Exception:
            log.warning("[notify] ask send failed", exc_info=True)
            return "timeout"

        try:
            return await asyncio.wait_for(result_fut, timeout=timeout_secs)
        except asyncio.TimeoutError:
            return "timeout"
