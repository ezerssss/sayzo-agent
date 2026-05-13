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
from typing import Any, Callable

from PIL import Image

from ..account import BLOCKED_ACCOUNT_STATES, CachedAccountStatus
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
    # Daily-drill EOD fallback. When the scheduler couldn't fire a
    # notification (mid-meeting all day, or backend unavailable), it
    # surfaces today's drill via this dynamic tray menu item. The label
    # is shown only when non-None; clicking sets ``eod_drill_event``
    # which the bridge polls and dispatches to the scheduler.
    _eod_drill_label: str | None = None
    eod_drill_event: threading.Event = field(default_factory=threading.Event)
    # Daily-drill manual test trigger (Debug submenu). Set by the tray
    # thread; the bridge polls and calls scheduler.fire_now(ignore_gates).
    test_drill_event: threading.Event = field(default_factory=threading.Event)

    # Last observed /api/me result, pushed here by the boot refresh task.
    # ``None`` ⇒ never checked yet; the tray renders a normal armed /
    # disarmed UI in that case (the arm-time gate is forgiving on
    # missing-cache too — see decide_arm_gate).
    _cached_account: CachedAccountStatus | None = None
    # Tray-thread → asyncio-loop signal: user clicked the "Finish setup at
    # sayzo.app →" item, agent should open the cached onboarding_url.
    finish_setup_event: threading.Event = field(default_factory=threading.Event)
    # Bootstrap flag — opt-in (default False so existing tests / CLI
    # paths that don't go through the slow boot remain unchanged). When
    # the live ``service()`` boot path constructs TrayState, it sets
    # this True before painting the tray, then calls ``mark_ready()``
    # once heavy imports + Agent construction finish on the asyncio
    # worker. While True, the tray tooltip + arm-toggle label render
    # "Starting…" so the user sees immediate visual feedback instead of
    # staring at a tray icon whose menu would silently no-op.
    _starting: bool = False

    # Shared notifier reference set by ``_build_pipeline_state`` once the
    # HUD launcher has been constructed. Read by anything that needs to
    # surface a toast outside the asyncio loop's normal flow — e.g. the
    # pre-quit hook below.
    notifier: Any = None

    # Optional callable invoked by :func:`request_full_shutdown` before the
    # quit event fires. Lets the agent surface a final-state notification
    # (e.g. "Sayzo is updating…" when a staged auto-update is about to be
    # applied) from a closure that has cfg / __version__ / notifier in
    # scope, without forcing tray.py to know about update_stage.
    pre_quit_hook: Callable[[], None] | None = None

    # Optional callable invoked when the user clicks the tray's "Install
    # Sayzo vX.Y.Z" menu item (and reused as the HUD "Install now" toast
    # button's ``on_pressed``). The closure — assigned by
    # ``_build_pipeline_state`` in ``__main__.py`` — writes the quit-apply
    # intent flag and calls :func:`request_full_shutdown` so the quit path
    # actually applies the staged update. Keeps tray.py free of update_apply
    # imports.
    on_install_update_clicked: Callable[[], None] | None = None

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

    def set_eod_drill_label(self, label: str | None) -> None:
        with self._lock:
            self._eod_drill_label = label

    def get_eod_drill_label(self) -> str | None:
        with self._lock:
            return self._eod_drill_label

    def is_starting(self) -> bool:
        with self._lock:
            return self._starting

    def mark_starting(self) -> None:
        """Set the bootstrap flag — tooltip + arm-toggle render
        ``"Starting…"`` until ``mark_ready()`` is called. Intended to be
        called by ``service()`` at tray construction, before the heavy
        ``.app`` / ``.notify`` import chain runs."""
        with self._lock:
            self._starting = True

    def mark_ready(self) -> None:
        """Clear the bootstrap flag once the live agent is wired up."""
        with self._lock:
            self._starting = False

    def set_cached_account(self, cached: CachedAccountStatus | None) -> bool:
        """Update the cached account snapshot. Returns ``True`` iff the
        gate-relevant state actually changed — callers use the return
        value to skip an otherwise wasted ``tray.update()`` rebuild."""
        with self._lock:
            prev = self._cached_account
            if prev is not None and cached is not None and (
                prev.account_state == cached.account_state
                and prev.onboarding_url == cached.onboarding_url
            ):
                return False
            if prev is None and cached is None:
                return False
            self._cached_account = cached
            return True

    def get_cached_account(self) -> CachedAccountStatus | None:
        with self._lock:
            return self._cached_account

    def is_account_blocked(self) -> bool:
        with self._lock:
            return (
                self._cached_account is not None
                and self._cached_account.account_state in BLOCKED_ACCOUNT_STATES
            )


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
    current user session; the SMAppService registration (or, on a
    pre-v2.7.0 upgrade still in progress, the legacy ``~/Library/
    LaunchAgents/`` plist) persists, so auto-start still works at next
    login.

    Uses the label-only form ``launchctl bootout gui/<uid>/<label>`` so
    the same call works whether the plist lives inside the app bundle
    (SMAppService, v2.7.0+) or in the user's home (legacy v2.6.x and
    earlier — only relevant during the upgrade window where this process
    is still supervised by the legacy registration).

    Best-effort: any failure is logged and swallowed so Quit still exits.
    """
    if sys.platform != "darwin":
        return
    try:
        uid = os.getuid()
    except AttributeError:
        return
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/com.sayzo.agent"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        log.exception("launchctl bootout failed")


def request_full_shutdown(state: "TrayState") -> None:
    """Ask the agent to fully exit — the same shape the tray Quit menu uses.

    macOS unloads launchd first; the eventual SIGKILL→process-group exit
    is non-zero, which would otherwise trip ``KeepAlive`` and revive us.

    Fires ``state.pre_quit_hook`` (if set) before signalling the quit so
    the agent has a chance to surface a "Sayzo is updating…" toast when
    this quit is going to trigger a staged auto-update apply.
    """
    if state.pre_quit_hook is not None:
        try:
            state.pre_quit_hook()
        except Exception:
            logging.getLogger(__name__).warning(
                "pre_quit_hook raised", exc_info=True,
            )
    _mac_unload_launchd_agent()
    state.quit_event.set()


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
        """Refresh the icon, tooltip, and menu labels. Call from any thread.

        Always rebuilds the menu — pystray's macOS backend evaluates
        ``MenuItem.text`` callables **once** at menu-construction time
        (``initWithTitle_action_keyEquivalent_`` in ``pystray/_darwin.py``
        sets a fixed string), so dynamic labels like the arm/stop hotkey
        only refresh when ``update_menu()`` actually re-runs the
        callable. The earlier short-circuit ("text re-evaluates on menu
        open") was wrong on macOS — confirmed by reading the pystray
        source — and produced the bug where the tray menu kept showing
        the default hotkey after the user changed it in Settings.

        Threading:

        - **Windows** — pystray's right-click handler rebuilds the popup
          fresh, so ``update_menu`` is technically a no-op for *menu*
          purposes; we still call it for the icon/tooltip mutation paths.
        - **Linux/GTK** — long-lived menu, ``update_menu`` is required.
        - **macOS** — NSStatusItem / NSMenu / NSMenuItem mutation has to
          happen on the AppKit main thread. Pystray's ``run_main`` is
          blocked in ``NSApp.run()`` on that thread, so we marshal via
          ``PyObjCTools.AppHelper.callAfter`` (which posts onto NSApp's
          runloop). Calling Cocoa methods from asyncio's background
          thread directly would crash or silently corrupt state.
        """
        if self._icon is None:
            return
        status, error_msg = self.state.get_status()
        status_changed = status != self._current_status
        self._current_status = status

        if sys.platform == "darwin":
            try:
                from PyObjCTools.AppHelper import (  # type: ignore[import-not-found]
                    callAfter,
                )
            except Exception:
                log.debug("[tray] PyObjCTools unavailable", exc_info=True)
                return

            def _refresh_on_main_thread() -> None:
                # All three touch NSObjects → main thread only.
                try:
                    self._icon.title = self._tooltip_for(status, error_msg)
                except Exception:
                    log.debug("[tray] title set failed", exc_info=True)
                try:
                    self._icon.update_menu()
                except Exception:
                    log.debug("[tray] update_menu failed", exc_info=True)
                if status_changed:
                    try:
                        self._icon.icon = _make_icon(status)
                    except Exception:
                        log.debug("[tray] icon set failed", exc_info=True)

            callAfter(_refresh_on_main_thread)
            return

        # Windows / Linux: direct call. Pystray handles its own internal
        # threading on these backends.
        if status_changed:
            self._icon.icon = _make_icon(status)
        self._icon.title = self._tooltip_for(status, error_msg)
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
        # Boot-time precedence: while heavy imports + Agent construction
        # are still running, neither the armed-state nor account-blocked
        # tooltip is meaningful — the user just opened Sayzo and needs to
        # know we're alive. Cleared by the asyncio bootstrap calling
        # ``state.mark_ready()`` after Agent is constructed.
        if self.state.is_starting():
            return "Sayzo — starting…"
        if status == Status.ERROR and error_msg:
            return f"Sayzo — {error_msg}"
        # Account-blocked supersedes everything except an active arming —
        # if we're already capturing we don't pull the rug out (the gate
        # only fires at the next arm attempt). Disarmed-and-blocked is
        # the common case where this matters.
        if status != Status.ARMED and self.state.is_account_blocked():
            return "Sayzo — finish setup at sayzo.app to start recording."
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
            request_full_shutdown(self.state)
            icon.stop()

        def on_open_update(icon, item):
            if self.state.get_update_offer() is None:
                return
            self.state.on_install_update_clicked()

        # ---- dynamic text callbacks (re-evaluated each menu open) ---------

        def arm_label(item) -> str:
            # Bootstrap: tray paints before Agent exists. Setting
            # ``arm_toggle_event`` while starting=True would queue an arm
            # against a controller that isn't ready yet — confusing
            # "click did nothing" UX. The bridge does eventually drain
            # the event when ready, but the label above the click is the
            # honest story for the user.
            if self.state.is_starting():
                return "Starting Sayzo…"
            status_now, _ = self.state.get_status()
            hotkey = self.state.get_hotkey_display()
            if status_now == Status.ARMED:
                return f"Stop recording   ({hotkey})"
            # When the account isn't ready, the click still routes through
            # the gate (which fires the toast). pystray can't reliably
            # disable items across backends, so we re-label instead — the
            # label is the truth about what'll happen on click.
            if self.state.is_account_blocked():
                return "Finish setup to record"
            # "Start recording" keeps parity with the hotkey confirmation
            # toast and the Stop label — avoids the "Arm Sayzo" jargon, which
            # reads as technical to a non-dev user.
            return f"Start recording   ({hotkey})"

        def update_text(item) -> str:
            offer = self.state.get_update_offer()
            return f"Install Sayzo v{offer.version}" if offer else ""

        def update_visible(item) -> bool:
            return self.state.get_update_offer() is not None

        def eod_drill_text(item) -> str:
            return self.state.get_eod_drill_label() or ""

        def eod_drill_visible(item) -> bool:
            return bool(self.state.get_eod_drill_label())

        def on_eod_drill(icon, item):
            self.state.eod_drill_event.set()

        def on_test_drill(icon, item):
            self.state.test_drill_event.set()

        def finish_setup_visible(item) -> bool:
            return self.state.is_account_blocked()

        def on_finish_setup(icon, item):
            # Open the cached onboarding URL on the asyncio loop side via
            # the bridge — keeps webbrowser.open off the tray thread (no
            # known issues, just consistent with how settings_event is
            # handled).
            self.state.finish_setup_event.set()

        # The Debug submenu only renders when SAYZO_DEBUG_TRAY=1 is in the
        # env — keeps the production menu clean. The CLI command
        # `sayzo-agent test-drill-notification` is the supported path for
        # most users; the menu item is convenience for hands-on QA.
        debug_visible = bool(os.environ.get("SAYZO_DEBUG_TRAY"))

        # NOTE: no ``default=True`` on the arm toggle — a single left-click
        # on the tray icon should NOT silently arm/disarm. The user reported
        # accidentally kicking off a recording by clicking the icon, then
        # seeing a stale menu label on the next open ("still says Start
        # recording" even though we were already armed). Requiring an
        # explicit menu click makes the intent unambiguous and lets the
        # label always be the ground truth for what happens next.
        menu = pystray.Menu(
            # Account-blocked CTA sits ABOVE the arm row so a user staring
            # at the menu after a denied hotkey press lands on it first.
            # Visible only while the cached account_state is one of the
            # three blocked values; invisible (zero-height) otherwise.
            pystray.MenuItem(
                "Finish setup at sayzo.app",
                on_finish_setup,
                visible=finish_setup_visible,
            ),
            pystray.MenuItem(arm_label, on_arm_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                eod_drill_text, on_eod_drill, visible=eod_drill_visible,
            ),
            pystray.MenuItem(update_text, on_open_update, visible=update_visible),
            pystray.MenuItem("Settings...", on_settings),
            pystray.MenuItem("Open captures folder", on_open_captures),
            pystray.MenuItem(
                "Test daily drill notification",
                on_test_drill,
                visible=lambda item: debug_visible,
            ),
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
