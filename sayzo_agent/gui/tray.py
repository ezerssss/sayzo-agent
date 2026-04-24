"""System tray icon for the background service (armed-only model, v1.0+).

Runs pystray on its own thread alongside the asyncio Agent loop. The shared
:class:`TrayState` dataclass is the communication surface — the tray thread
sets ``arm_toggle_event`` / ``quit_event`` / ``settings_event``, and the
asyncio loop's ``_tray_bridge`` polls them and calls into the
``ArmController`` accordingly.

Menu layout:

    Disarmed:
        Start recording   (Ctrl+Alt+S)
        ---
        Settings...
        Open captures folder
        ---
        Quit Sayzo

    Armed:
        Stop recording   (Ctrl+Alt+S)
        ---
        (same tail as disarmed)

The top item is NOT a pystray ``default`` item — a bare left-click on the
icon must not arm/disarm. The user has to open the menu and click the
label explicitly so the action always matches what the label promises.

Tray menu clicks are treated as final by ``ArmController.arm_from_tray``:
no additional confirmation toast fires on either arm or disarm (the label
IS the confirmation). Hotkey transitions still confirm via toast.

The hotkey string is interpolated at render time so the menu always reflects
the user's current binding (even after a rebind via the Settings window).
The "Reopen setup" menu item that used to open a separate tkinter
walkthrough is gone — the pywebview first-run window now covers everything,
and the Settings window is the post-setup surface for hotkey/permissions
tweaks.
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .fs import open_folder

log = logging.getLogger(__name__)

# Tray icon size (pixels). pystray rescales as needed for the host OS, so we
# pass a reasonably high-resolution PIL image and let it figure out the rest.
_ICON_SIZE = 128


# ---------------------------------------------------------------------------
# Shared state between asyncio loop and tray thread
# ---------------------------------------------------------------------------

class Status(enum.Enum):
    """Armed state as rendered in the tray.

    Value strings double as the tooltip suffix: ``"armed"`` / ``"disarmed"``.
    ``error`` is set when capture stream acquisition failed on arm. Legacy
    ``LISTENING`` / ``PAUSED`` values are retained so older callers still
    resolve, but the tray logic only branches on ARMED vs DISARMED now.
    """

    ARMED = "armed"
    DISARMED = "disarmed"
    ERROR = "error"
    # Back-compat aliases — older callers (e.g. test helpers) may still
    # reference these. They all render as DISARMED in the menu.
    LISTENING = "listening"
    PAUSED = "paused"
    SETTING_UP = "setting_up"


@dataclass(frozen=True)
class UpdateOffer:
    """Advertised newer version of the agent, surfaced in the tray menu."""

    version: str
    url: str


@dataclass
class TrayState:
    """Thread-safe state shared between the agent loop and the tray icon.

    **Tray-thread → asyncio-loop signals**:
    - ``arm_toggle_event`` — fired when user clicks the top Arm/Stop item.
      The ``_tray_bridge`` in ``__main__.py`` calls
      ``arm_controller.arm_from_tray()`` on the asyncio loop.
    - ``settings_event`` — "Settings..." clicked; opens the settings GUI.
    - ``quit_event`` — "Quit Sayzo" clicked; agent shuts down.

    **Asyncio-loop → tray-thread signals** (kept in sync by the bridge):
    - ``status`` — ARMED / DISARMED / ERROR. Drives the top-menu label.
    - ``hotkey_display`` — current hotkey binding as a human string
      (``"Ctrl+Alt+S"``) interpolated into the menu labels.
    """

    status: Status = Status.DISARMED
    error_message: str = ""
    hotkey_display: str = "Ctrl+Alt+S"
    _lock: threading.Lock = field(default_factory=threading.Lock)
    arm_toggle_event: threading.Event = field(default_factory=threading.Event)
    settings_event: threading.Event = field(default_factory=threading.Event)
    quit_event: threading.Event = field(default_factory=threading.Event)
    # Legacy pause-event — kept for back-compat with CLI entrypoints that
    # still reference it. Unused by the tray menu itself.
    pause_event: threading.Event = field(default_factory=threading.Event)
    _update_offer: UpdateOffer | None = None

    def set_status(self, status: Status, error_message: str = "") -> None:
        with self._lock:
            self.status = status
            self.error_message = error_message

    def get_status(self) -> tuple[Status, str]:
        with self._lock:
            return self.status, self.error_message

    def set_hotkey_display(self, hotkey: str) -> None:
        with self._lock:
            self.hotkey_display = hotkey

    def get_hotkey_display(self) -> str:
        with self._lock:
            return self.hotkey_display

    def set_update_offer(self, offer: UpdateOffer | None) -> None:
        with self._lock:
            self._update_offer = offer

    def get_update_offer(self) -> UpdateOffer | None:
        with self._lock:
            return self._update_offer


# ---------------------------------------------------------------------------
# Tray icon — loads the Sayzo logo from the bundled assets directory. Status
# indication lives in the tooltip title + menu labels.
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
    change per status. Users get status info via tooltip + menu labels.
    """
    global _cached_icon
    if _cached_icon is not None:
        return _cached_icon

    path = _logo_path()
    try:
        img = Image.open(path).convert("RGBA")
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
        opened, so arm/disarm labels stay correct.
        """
        if self._icon is None:
            return
        status, error_msg = self.state.get_status()
        if status == self._current_status:
            return
        self._current_status = status
        if sys.platform == "darwin":
            # AppKit NSMenu mutation must happen on main thread; we're on
            # asyncio's background thread here. The menu item's `text`
            # callback re-evaluates on each menu open, so labels still
            # refresh — just via pystray's own menu-rebuild path instead.
            return
        self._icon.icon = _make_icon(status)
        self._icon.title = self._tooltip_for(status, error_msg)
        # Pystray on Windows is a no-op (menu is rebuilt on each right-
        # click), but on Linux/GTK the native menu is long-lived and needs
        # this kick so callable `text`/`visible` get re-evaluated.
        try:
            self._icon.update_menu()
        except Exception:
            log.debug("tray update_menu failed (non-fatal)", exc_info=True)

    def stop(self) -> None:
        """Stop the tray icon. Safe to call from any thread."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                log.exception("tray stop failed")

    # -- internal ----------------------------------------------------------

    def _tooltip_for(self, status: Status, error_msg: str) -> str:
        hotkey = self.state.get_hotkey_display()
        if status == Status.ERROR and error_msg:
            return f"Sayzo — {error_msg}"
        if status == Status.ARMED:
            return f"Sayzo — capturing. Press {hotkey} to stop."
        # Everything else (DISARMED and legacy aliases) → disarmed tooltip.
        # Phrasing conveys the armed-only invariant: the mic is off right
        # now, and a capture only starts when the user explicitly arms it
        # or accepts a meeting-detect prompt.
        return f"Sayzo — mic off. Press {hotkey} to start, or we'll ask when you're in a meeting."

    def _run(self) -> None:
        import pystray

        status, _ = self.state.get_status()
        self._current_status = status

        # ---- menu callbacks (run on the tray thread) -----------------------

        def on_arm_toggle(icon, item):
            """Top-menu click: signal the asyncio loop to arm or disarm."""
            self.state.arm_toggle_event.set()

        def on_settings(icon, item):
            self.state.settings_event.set()

        def on_open_captures(icon, item):
            self.captures_dir.mkdir(parents=True, exist_ok=True)
            open_folder(self.captures_dir)

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

        # ---- dynamic text callbacks (re-evaluated each menu open) ---------

        def arm_label(item) -> str:
            status_now, _ = self.state.get_status()
            hotkey = self.state.get_hotkey_display()
            if status_now == Status.ARMED:
                return f"Stop recording   ({hotkey})"
            # "Start recording" keeps parity with the hotkey confirmation
            # toast and the Stop label — avoids the "Arm Sayzo" jargon, which
            # reads as technical to a non-dev user.
            return f"Start recording   ({hotkey})"

        def update_text(item) -> str:
            offer = self.state.get_update_offer()
            return f"Download Sayzo v{offer.version}" if offer else ""

        def update_visible(item) -> bool:
            return self.state.get_update_offer() is not None

        # NOTE: no ``default=True`` on the arm toggle — a single left-click
        # on the tray icon should NOT silently arm/disarm. The user reported
        # accidentally kicking off a recording by clicking the icon, then
        # seeing a stale menu label on the next open ("still says Start
        # recording" even though we were already armed). Requiring an
        # explicit menu click makes the intent unambiguous and lets the
        # label always be the ground truth for what happens next.
        menu = pystray.Menu(
            pystray.MenuItem(arm_label, on_arm_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(update_text, on_open_update, visible=update_visible),
            pystray.MenuItem("Settings...", on_settings),
            pystray.MenuItem("Open captures folder", on_open_captures),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Sayzo", on_quit),
        )

        self._icon = pystray.Icon(
            name="sayzo-agent",
            icon=_make_icon(status),
            title=self._tooltip_for(status, ""),
            menu=menu,
        )

        log.info("tray icon starting")
        self._icon.run()
        log.info("tray icon stopped")
