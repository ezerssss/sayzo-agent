"""System tray icon for the background service.

Runs pystray on its own thread alongside the asyncio Agent loop.  Communication
is via a shared :class:`TrayState` dataclass protected by a threading lock.
"""
from __future__ import annotations

import enum
import logging
import os
import platform
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Tray icon size (pixels). pystray rescales as needed for the host OS, so we
# pass a reasonably high-resolution PIL image and let it figure out the rest.
_ICON_SIZE = 128


# ---------------------------------------------------------------------------
# Shared state between asyncio loop and tray thread
# ---------------------------------------------------------------------------

class Status(enum.Enum):
    LISTENING = "listening"
    PAUSED = "paused"
    SETTING_UP = "setting_up"
    ERROR = "error"


@dataclass(frozen=True)
class UpdateOffer:
    """Advertised newer version of the agent, surfaced in the tray menu."""

    version: str
    url: str


@dataclass
class TrayState:
    """Thread-safe state shared between the agent loop and the tray icon."""

    status: Status = Status.LISTENING
    error_message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    pause_event: threading.Event = field(default_factory=threading.Event)
    quit_event: threading.Event = field(default_factory=threading.Event)
    _update_offer: UpdateOffer | None = None

    def set_status(self, status: Status, error_message: str = "") -> None:
        with self._lock:
            self.status = status
            self.error_message = error_message

    def get_status(self) -> tuple[Status, str]:
        with self._lock:
            return self.status, self.error_message

    def set_update_offer(self, offer: UpdateOffer | None) -> None:
        with self._lock:
            self._update_offer = offer

    def get_update_offer(self) -> UpdateOffer | None:
        with self._lock:
            return self._update_offer


# ---------------------------------------------------------------------------
# Tray icon — loads the Sayzo logo from the bundled assets directory. Status
# indication lives in the tooltip title + menu labels (no more colored
# circles, per user request — the green dot felt unsettling).
# ---------------------------------------------------------------------------


def _logo_path() -> Path:
    """Resolve the bundled logo image path for dev and frozen builds."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    else:
        # tray.py is sayzo_agent/gui/tray.py — climb to repo root.
        base = Path(__file__).resolve().parent.parent.parent / "installer" / "assets"
    return base / "logo.png"


_cached_icon: Image.Image | None = None


def _make_icon(status: Status) -> Image.Image:
    """Return the Sayzo logo as a PIL image, cached on first load.

    ``status`` is accepted for signature compatibility — the visual doesn't
    change per status anymore. Users get status info via tooltip + menu.
    """
    global _cached_icon
    if _cached_icon is not None:
        return _cached_icon

    path = _logo_path()
    try:
        img = Image.open(path).convert("RGBA")
        # pystray on macOS expects reasonably small/square icons; scale down
        # big PNGs to _ICON_SIZE so we don't bloat memory.
        if max(img.size) > _ICON_SIZE:
            img.thumbnail((_ICON_SIZE, _ICON_SIZE), Image.Resampling.LANCZOS)
        _cached_icon = img
    except (OSError, FileNotFoundError):
        log.warning("could not load tray logo at %s; using blank fallback", path)
        _cached_icon = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    return _cached_icon


# ---------------------------------------------------------------------------
# Tray icon (pystray)
# ---------------------------------------------------------------------------

def _open_folder(path: Path) -> None:
    """Open a folder in the platform's file manager."""
    p = str(path)
    if sys.platform == "win32":
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def _mac_unload_launchd_agent() -> None:
    """Ask launchd to stop supervising the agent before we exit.

    Without this, launchd's ``KeepAlive`` resurrects the process seconds
    after a menu-bar Quit — the user sees the app close and instantly
    reopen. ``bootout`` removes the job from launchd's registry for the
    current user session; the plist stays on disk and re-loads on next
    login, so auto-start still works tomorrow.

    Falls back to the legacy ``launchctl unload`` form on older macOS.
    Best-effort: any failure is logged and swallowed so Quit still exits.
    """
    if sys.platform != "darwin":
        return
    plist = Path.home() / "Library" / "LaunchAgents" / "com.sayzo.agent.plist"
    if not plist.exists():
        return
    try:
        uid = os.getuid()
    except AttributeError:
        return
    try:
        r = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(plist)],
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            subprocess.run(
                ["launchctl", "unload", str(plist)],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        log.exception("launchctl unload failed")


class TrayIcon:
    """Manages the system tray icon lifecycle on a background thread."""

    def __init__(self, state: TrayState, captures_dir: Path) -> None:
        self.state = state
        self.captures_dir = captures_dir
        self._icon = None
        self._thread: threading.Thread | None = None
        self._current_status: Status | None = None

    def start(self) -> None:
        """Start the tray icon on a daemon thread.

        Safe on Windows/Linux. On macOS pystray requires the main thread
        (AppKit instantiates NSStatusItem/NSWindow) — use :meth:`run_main`
        there instead.
        """
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()

    def run_main(self) -> None:
        """Run the tray icon on the calling (main) thread — blocks until stop().

        Required on macOS. Caller must have moved the asyncio loop to a
        background thread first.
        """
        self._run()

    def update(self) -> None:
        """Refresh the icon/tooltip if the status changed. Call from any thread.

        On macOS, icon/title mutation touches AppKit objects which is only
        safe on the main thread. We skip cross-thread mutation there; the
        menu text callbacks still re-evaluate dynamically when the menu is
        opened, so pause/resume labels stay correct.
        """
        if self._icon is None:
            return
        status, error_msg = self.state.get_status()
        if status == self._current_status:
            return
        self._current_status = status
        if sys.platform == "darwin":
            return
        self._icon.icon = _make_icon(status)
        if status == Status.ERROR and error_msg:
            self._icon.title = f"Sayzo Agent — {error_msg}"
        else:
            self._icon.title = f"Sayzo Agent — {status.value.replace('_', ' ').title()}"

    def stop(self) -> None:
        """Stop the tray icon. Safe to call from any thread."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.exception("tray stop failed")

    # -- internal ----------------------------------------------------------

    def _run(self) -> None:
        import pystray

        status, _ = self.state.get_status()
        self._current_status = status

        def on_pause_resume(icon, item):
            if self.state.pause_event.is_set():
                self.state.pause_event.clear()
                self.state.set_status(Status.LISTENING)
            else:
                self.state.pause_event.set()
                self.state.set_status(Status.PAUSED)
            self.update()

        def on_open_captures(icon, item):
            self.captures_dir.mkdir(parents=True, exist_ok=True)
            _open_folder(self.captures_dir)

        def on_quit(icon, item):
            _mac_unload_launchd_agent()
            self.state.quit_event.set()
            icon.stop()

        def on_open_update(icon, item):
            offer = self.state.get_update_offer()
            if offer is None:
                return
            import webbrowser
            webbrowser.open(offer.url)

        def pause_text(item):
            return "Resume" if self.state.pause_event.is_set() else "Pause"

        def status_text(item):
            s, err = self.state.get_status()
            if s == Status.ERROR and err:
                return f"Status: {err}"
            return f"Status: {s.value.replace('_', ' ').title()}"

        def update_text(item):
            offer = self.state.get_update_offer()
            return f"Download Sayzo v{offer.version}" if offer else ""

        def update_visible(item):
            return self.state.get_update_offer() is not None

        menu = pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            # Only rendered when the update-check task has surfaced a newer
            # version on TrayState. Click opens the platform-specific installer
            # URL in the user's browser; the existing installer (NSIS on Win,
            # DMG drag on Mac) handles the replace.
            pystray.MenuItem(update_text, on_open_update, visible=update_visible),
            pystray.MenuItem(pause_text, on_pause_resume),
            pystray.MenuItem("Open captures folder", on_open_captures),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )

        self._icon = pystray.Icon(
            name="sayzo-agent",
            icon=_make_icon(status),
            title=f"Sayzo Agent — {status.value.replace('_', ' ').title()}",
            menu=menu,
        )

        log.info("tray icon starting")
        self._icon.run()
        log.info("tray icon stopped")
