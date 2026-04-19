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

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

# Tray icon size (pixels).
_ICON_SIZE = 64


# ---------------------------------------------------------------------------
# Shared state between asyncio loop and tray thread
# ---------------------------------------------------------------------------

class Status(enum.Enum):
    LISTENING = "listening"
    PAUSED = "paused"
    SETTING_UP = "setting_up"
    ERROR = "error"


@dataclass
class TrayState:
    """Thread-safe state shared between the agent loop and the tray icon."""

    status: Status = Status.LISTENING
    error_message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    pause_event: threading.Event = field(default_factory=threading.Event)
    quit_event: threading.Event = field(default_factory=threading.Event)

    def set_status(self, status: Status, error_message: str = "") -> None:
        with self._lock:
            self.status = status
            self.error_message = error_message

    def get_status(self) -> tuple[Status, str]:
        with self._lock:
            return self.status, self.error_message


# ---------------------------------------------------------------------------
# Icon generation — simple colored circles, no external assets needed
# ---------------------------------------------------------------------------

_COLORS = {
    Status.LISTENING: "#22c55e",   # green
    Status.PAUSED: "#9ca3af",      # grey
    Status.SETTING_UP: "#eab308",  # yellow
    Status.ERROR: "#ef4444",       # red
}


def _make_icon(status: Status) -> Image.Image:
    """Generate a solid-circle icon for the given status."""
    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin],
        fill=_COLORS[status],
    )
    return img


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

        def pause_text(item):
            return "Resume" if self.state.pause_event.is_set() else "Pause"

        def status_text(item):
            s, err = self.state.get_status()
            if s == Status.ERROR and err:
                return f"Status: {err}"
            return f"Status: {s.value.replace('_', ' ').title()}"

        menu = pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
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
