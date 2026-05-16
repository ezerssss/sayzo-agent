"""HUD subprocess window — PySide6 + QtWebEngine.

A frameless `QWidget` host (`WA_TranslucentBackground`) wrapping a
`QWebEngineView` that renders the same React HUD bundle. JS↔Python
bridging via `QWebChannel`; see :class:`sayzo_agent.gui.hud.bridge.HudBridge`.

The public ``HudWindow(cfg, demo).run_blocking()`` interface drives a
self-contained `QApplication.exec()` so the standalone preview script
and the ``sayzo-agent hud`` CLI can both spin up an HUD process.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from PySide6.QtCore import QObject, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from sayzo_agent.config import Config
from sayzo_agent.gui.common.assets import webui_index_path
from sayzo_agent.gui.hud.bridge import HudBridge

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo HUD"

# Initial host window size at construction time. React's ResizeObserver
# overrides this within one frame of mount via
# ``HudBridge.set_window_size``. The window is created at opacity 0
# so the initial 100×100 box is never seen.
INITIAL_HUD_WIDTH = 100
INITIAL_HUD_HEIGHT = 100

# Distance from the screen edge to the HUD's anchor corner.
HUD_EDGE_INSET = 8


def _hud_url(index: Path, *, demo: bool) -> str:
    """Build a ``file://…/index.html#route=hud[&demo=1]`` URL."""
    base = index.as_uri()
    params = {"route": "hud"}
    if demo:
        params["demo"] = "1"
    return f"{base}#{urlencode(params)}"


class _HudHostWidget(QWidget):
    """The actual Qt window. Encapsulates flags, geometry, web view."""

    # Emitted on each main-thread Qt event loop tick by a stdin reader.
    # The reader runs on a daemon thread and emits this signal so the
    # JS dispatch happens on the GUI thread (QWebEngineView is GUI-thread-only).
    _command_received = Signal(str)

    def __init__(self, cfg: Config, *, demo: bool) -> None:
        super().__init__()
        self._cfg = cfg
        self._demo = demo
        self._loaded_event = threading.Event()
        # Queued commands that arrive before ``loadFinished`` fires.
        self._pending_commands: list[str] = []
        self._pending_lock = threading.Lock()
        # If `loadFinished` never fires (page error, hung load),
        # the stdin reader would otherwise grow this list unbounded
        # — capping at 200 entries with FIFO drop keeps a wedged
        # subprocess from holding megabytes of stale audio-level
        # commands.
        self._pending_commands_cap = 200
        self._quitting = False
        # Last visibility / size decisions so duplicate calls are no-ops.
        self._currently_visible = False
        self._current_width = INITIAL_HUD_WIDTH
        self._current_height = INITIAL_HUD_HEIGHT
        # Top-right anchor of the chosen screen, in Qt coordinate space.
        # Refreshed on every visibility-show transition (see
        # :meth:`_refresh_screen_anchor`) so a late-arriving display
        # config — agent auto-starts before Windows finishes painting
        # the desktop, user docks a laptop mid-session, resolution
        # change — gets picked up on the next toast rather than
        # baking a stale value for the whole HUD lifetime. The
        # initial value here is the best guess at construction time
        # and seeds the first show.
        self._screen_right_edge, self._screen_top_edge = self._compute_screen_anchor()
        # Live "where the window's top-right corner currently sits"
        # anchor. Updated by ``moveEvent`` whenever the window moves
        # while visible (programmatic or user-initiated drag). On
        # resize (collapse pill → dot, expand dot → pill, toast added)
        # we pin the right edge to this and let the left edge slide,
        # so the window grows / shrinks toward the left rather than
        # snapping back to the screen's top-right corner if the user
        # has dragged it elsewhere.
        self._anchor_right_x = self._screen_right_edge
        self._anchor_y = self._screen_top_edge
        # Set while we're doing a programmatic move / resize, so
        # ``moveEvent`` only treats user-initiated drags as anchor
        # updates. Without this guard, programmatic ``setGeometry``
        # calls would clobber the anchor with stale geometry values
        # if Qt fires ``moveEvent`` before ``resizeEvent``.
        self._suppress_anchor_update = False
        # Bounded retry counter for the macOS overlay-tweak path —
        # NSWindow realization can lag Qt's loadFinished signal by a
        # tick under load. See :meth:`_apply_mac_overlay_tweaks`.
        self._overlay_tweak_attempts = 0

        # Frameless, top-most, no taskbar/Alt-Tab, no focus theft.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # Genuine per-pixel alpha — pixels the React app paints as
        # transparent become OS-level transparent.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Realize the native window at the real top-right anchor from
        # boot so macOS WindowServer establishes a CGS connection for
        # the subprocess immediately. Hide via opacity, NOT via offscreen
        # geometry — pre-v3.3.0 we set geometry to (-20000, 0) and let
        # ``_set_window_visible(true)`` move it on-screen when content
        # arrived. On macOS that left the NSWindow unrealized
        # (transparent frameless widget shown outside every screen ⇒
        # WindowServer never allocates a backing surface), which is why
        # the agent-spawned HUD never appeared even though
        # ``sayzo-agent hud --demo`` did (demo's URL hash forces
        # ``hasContent=true`` immediately, so the move-on-screen lands
        # before WindowServer commits to "unrealized" state).
        self.setWindowOpacity(0.0)
        init_x = self._screen_right_edge - INITIAL_HUD_WIDTH
        init_y = self._screen_top_edge
        self.setGeometry(
            init_x, init_y, INITIAL_HUD_WIDTH, INITIAL_HUD_HEIGHT,
        )
        self.setWindowTitle(WINDOW_TITLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QWebEngineView(self)
        # Chromium needs an explicit transparent page bg or it paints
        # an opaque white background underneath the React content,
        # defeating the WA_TranslucentBackground host.
        self._view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        layout.addWidget(self._view)

        # Bridge + WebChannel. We expose the bridge as ``hudPyBridge``
        # on the channel; the JS side reads it via ``QWebChannel``.
        self._bridge = HudBridge()
        self._channel = QWebChannel(self)
        self._channel.registerObject("hudPyBridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # Hook up load + command pipeline.
        self._view.loadFinished.connect(self._on_load_finished)
        self._command_received.connect(self._dispatch_command_on_gui_thread)

        # Listen for display-config changes so a docked-laptop /
        # plugged-in-monitor / resolution-change mid-session re-anchors
        # the HUD instead of leaving it on a screen that no longer
        # exists or at coordinates that no longer map anywhere visible.
        gui_app = QGuiApplication.instance()
        if gui_app is not None:
            gui_app.primaryScreenChanged.connect(self._on_screen_config_changed)
            gui_app.screenAdded.connect(self._on_screen_config_changed)
            gui_app.screenRemoved.connect(self._on_screen_config_changed)

        # One-time dump of every detected screen at HUD boot. Triages
        # the next "I can't see the HUD" report in one grep — without
        # this we have to ask the user to run a separate PowerShell
        # snippet to enumerate monitors.
        self._log_all_screens()

        # Load the React bundle.
        index = webui_index_path()
        if not index.exists():
            log.error("[hud] UI assets missing at %s — HUD will not render", index)
            return
        url = _hud_url(index, demo=self._demo)
        log.info(
            "[hud] opening Qt window at (x=%s y=%s opacity=0.0); right edge=%s top edge=%s "
            "initial w=%s h=%s demo=%s url=%s",
            init_x, init_y, self._screen_right_edge, self._screen_top_edge,
            INITIAL_HUD_WIDTH, INITIAL_HUD_HEIGHT, self._demo, url,
        )
        self._view.load(QUrl(url))
        log.info(
            "[hud] post-init state: isVisible=%s windowHandle=%s geometry=%s",
            self.isVisible(),
            self.windowHandle() is not None,
            self.geometry().getRect(),
        )

    @property
    def bridge(self) -> HudBridge:
        return self._bridge

    # ------------------------------------------------------------------
    # Lifecycle hooks.
    # ------------------------------------------------------------------

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            log.warning("[hud] page load reported not-ok")
        log.info(
            "[hud] loadFinished: ok=%s isVisible=%s windowHandle=%s geometry=%s",
            ok,
            self.isVisible(),
            self.windowHandle() is not None,
            self.geometry().getRect(),
        )
        # Apply macOS-specific overlay tweaks (status-window-level,
        # collection behaviour, hides-on-deactivate=False) once Qt has
        # realised the native NSWindow. Probe lsappinfo first — that
        # single line is the kill-criterion for "did macOS WindowServer
        # actually register us?" and would have shortcut the entire
        # v3.1.6 → v3.2.1 LaunchServices chase if we'd been logging it.
        if sys.platform == "darwin":
            self._log_lsappinfo_self()
            self._apply_mac_overlay_tweaks()
        # Wire the bridge callbacks now that the host widget is real.
        # React may already have buffered set_window_visible /
        # set_window_size requests via the bridge's pending buffers —
        # those replay automatically on registration.
        self._bridge.set_visibility_callback(self._set_window_visible)
        self._bridge.set_size_callback(self._set_window_size)
        self._bridge.set_start_system_move_callback(self._start_system_move)
        self._loaded_event.set()
        # Drain any commands that landed before the page was ready.
        self._flush_pending_commands()

    # ------------------------------------------------------------------
    # Visibility — toggle via setWindowOpacity(0/1). The NSWindow stays
    # realized for the entire HUD lifecycle; moving it offscreen would
    # cause WindowServer on macOS to drop the backing surface (the
    # original v3.0-era bug that v3.3.0 fixes). No focus stealing
    # because the window has Qt.WindowDoesNotAcceptFocus.
    # ------------------------------------------------------------------

    def _set_window_visible(self, visible: bool) -> None:
        if visible == self._currently_visible:
            return
        self._currently_visible = visible
        if visible:
            # Refresh the chosen-screen anchor on every show. The value
            # captured at HUD-subprocess __init__ is suspect in two
            # common scenarios — agent auto-starts at boot before
            # Windows finishes painting the desktop, and the user
            # reconfigures monitors between captures — so we re-probe
            # Qt's screen API here. After this, even if the init-time
            # value was wrong, the very first show lands the window
            # correctly.
            self._refresh_screen_anchor()
            # Snap the live anchor back to the freshly-computed
            # top-right of the chosen screen. The user-drag override in
            # ``moveEvent`` only persists within a single visible
            # session — once the previous batch cleared and we went
            # hidden, the next cycle starts fresh at top-right.
            self._anchor_right_x = self._screen_right_edge
            self._anchor_y = self._screen_top_edge
            x = self._anchor_right_x - self._current_width
            y = self._anchor_y
            # Last-resort safety net: if for some reason the target
            # rect lies outside every detected screen (screen was
            # removed between refresh and move, fallback values are
            # stale, …), clamp to a primary-screen position the user
            # can definitely see.
            x, y = self._clamp_to_visible_screen(x, y)
            self._suppress_anchor_update = True
            try:
                self.move(int(x), int(y))
            finally:
                self._suppress_anchor_update = False
            self.setWindowOpacity(1.0)
            log.info(
                "[hud] window visibility → shown (pos=%d,%d size=%dx%d opacity=1.0)",
                int(x), int(y), self._current_width, self._current_height,
            )
        else:
            # Going hidden: fade out via opacity, leave geometry in
            # place. Reset the anchor back to the screen's top-right so
            # the NEXT visibility cycle starts fresh at the top-right
            # corner. Without this the window would re-appear wherever
            # the user dragged it last session, which on a "no content
            # → new content" transition feels like a stale state from
            # the previous arm cycle.
            self._anchor_right_x = self._screen_right_edge
            self._anchor_y = self._screen_top_edge
            self.setWindowOpacity(0.0)
            log.info(
                "[hud] window visibility → hidden (opacity=0.0, geometry unchanged)",
            )

    def _set_window_size(self, width: int, height: int) -> None:
        if width == self._current_width and height == self._current_height:
            return
        self._current_width = int(width)
        self._current_height = int(height)
        # Pin the RIGHT edge to the live anchor so the window grows /
        # shrinks toward the LEFT. Collapse (pill → dot) shrinks toward
        # the right edge of the user's current window position instead
        # of snapping back to the screen corner; expand (dot → pill)
        # grows leftward from the dot's current right edge instead of
        # overflowing off the right of the monitor. Always pin against
        # the live anchor — when hidden the window stays at the same
        # geometry (just at opacity 0), so the anchor math is the same.
        x = self._anchor_right_x - self._current_width
        y = self._anchor_y
        # Resize can push the rect off-screen too (e.g. window widened
        # past the right edge of a narrow screen); apply the same
        # safety clamp the visibility path uses.
        x, y = self._clamp_to_visible_screen(x, y)
        self._suppress_anchor_update = True
        try:
            self.setGeometry(
                int(x), int(y), self._current_width, self._current_height,
            )
        finally:
            self._suppress_anchor_update = False
        log.info(
            "[hud] window size → %dx%d (visible=%s)",
            self._current_width, self._current_height, self._currently_visible,
        )

    def moveEvent(self, event) -> None:  # noqa: ANN001 — Qt signature
        """Track the window's right edge so future resizes keep it pinned.

        Qt fires ``moveEvent`` on every position change — both
        programmatic ``self.move()`` / ``self.setGeometry()`` and
        user-initiated drags via ``startSystemMove``. We update the
        live anchor only when we're NOT in the middle of a
        programmatic call (``_suppress_anchor_update`` is set in
        ``_set_window_visible`` / ``_set_window_size``) so the anchor
        only reflects what the user explicitly did with a drag.
        Without this guard, ``moveEvent`` firing before ``resizeEvent``
        during a programmatic ``setGeometry`` would compute the
        anchor against the OLD width and produce a glitched value.
        """
        super().moveEvent(event)
        if self._currently_visible and not self._suppress_anchor_update:
            self._anchor_right_x = self.x() + self.width()
            self._anchor_y = self.y()

    def _start_system_move(self) -> None:
        """Hand off a window drag to the OS at the cursor's current position.

        Routed here from the React mousedown handler via the bridge's
        ``start_system_move`` slot. ``QWindow.startSystemMove()`` lets
        the OS take over cursor tracking, snapping, and release —
        native drag behaviour for a frameless window.
        """
        handle = self.windowHandle()
        if handle is None:
            log.warning("[hud] startSystemMove: no windowHandle")
            return
        try:
            handle.startSystemMove()
        except Exception:
            log.warning("[hud] startSystemMove failed", exc_info=True)

    # ------------------------------------------------------------------
    # Screen-anchor management. Used by ``_set_window_visible`` and
    # the screen-change signal handlers to keep the HUD on a screen
    # the user can actually see across boot-time display races,
    # docking / undocking, resolution changes, and monitor unplug.
    # ------------------------------------------------------------------

    def _compute_screen_anchor(self) -> tuple[int, int]:
        """Return ``(right_x, top_y)`` of the chosen screen's anchor.

        Uses Qt's screen API rather than Win32 ``GetSystemMetrics``:
        the value is in the same coordinate space ``setGeometry()``
        interprets, so we don't get a DPI mismatch on non-100% scale
        monitors. Picks the screen under the cursor (with primary as
        fallback) so a toast lands on whichever monitor the user is
        actively working on rather than always on primary.

        Returns ``(1280, HUD_EDGE_INSET)`` as a last-ditch fallback
        if no QGuiApplication / no screens are available; the
        sanity-clamp in ``_clamp_to_visible_screen`` catches any
        case where that fallback is actually offscreen.
        """
        gui_app = QGuiApplication.instance()
        if gui_app is None:
            log.warning("[hud] _compute_screen_anchor: no QGuiApplication yet")
            return 1280, HUD_EDGE_INSET
        cursor_pos = QCursor.pos()
        screen = gui_app.screenAt(cursor_pos) or gui_app.primaryScreen()
        if screen is None:
            log.warning("[hud] _compute_screen_anchor: no screens detected")
            return 1280, HUD_EDGE_INSET
        geom = screen.availableGeometry()
        right_x = geom.right() - HUD_EDGE_INSET
        top_y = geom.top() + HUD_EDGE_INSET
        log.info(
            "[hud] screen anchor: name=%s availableGeometry=(%d,%d %dx%d) "
            "cursor=(%d,%d) → right_x=%d top_y=%d",
            screen.name(),
            geom.x(), geom.y(), geom.width(), geom.height(),
            cursor_pos.x(), cursor_pos.y(),
            right_x, top_y,
        )
        return right_x, top_y

    def _refresh_screen_anchor(self) -> None:
        """Re-probe Qt for the chosen screen and store the result."""
        self._screen_right_edge, self._screen_top_edge = self._compute_screen_anchor()

    def _log_all_screens(self) -> None:
        """One-time diagnostic dump of every detected screen at HUD boot.

        Logged once in ``__init__`` so any future "I can't see the HUD"
        report has the full display topology available without a
        follow-up PowerShell snippet. Includes per-screen DPI so we can
        spot scaling-related misplacements.
        """
        gui_app = QGuiApplication.instance()
        if gui_app is None:
            log.warning("[hud] _log_all_screens: no QGuiApplication")
            return
        primary = gui_app.primaryScreen()
        screens = gui_app.screens()
        log.info("[hud] screens detected: count=%d", len(screens))
        for screen in screens:
            full = screen.geometry()
            avail = screen.availableGeometry()
            log.info(
                "[hud]   screen name=%s primary=%s "
                "geometry=(%d,%d %dx%d) available=(%d,%d %dx%d) "
                "devicePixelRatio=%.2f logicalDpi=%.0f",
                screen.name(), screen is primary,
                full.x(), full.y(), full.width(), full.height(),
                avail.x(), avail.y(), avail.width(), avail.height(),
                screen.devicePixelRatio(), screen.logicalDotsPerInch(),
            )

    def _clamp_to_visible_screen(self, x: int, y: int) -> tuple[int, int]:
        """Ensure ``(x, y, current_w, current_h)`` overlaps at least one screen.

        If the target rect lies entirely outside every screen's
        ``availableGeometry``, fall back to the primary screen's
        top-left plus ``HUD_EDGE_INSET``. This is the last-resort
        safety net — every layer above (Qt-coord-space probe,
        recompute-on-show, screen-change signal handler) should
        already keep us on a visible screen, but if something has
        regressed at least the HUD lands somewhere the user can
        find it.
        """
        gui_app = QGuiApplication.instance()
        if gui_app is None:
            return x, y
        rect = QRect(int(x), int(y), self._current_width, self._current_height)
        for screen in gui_app.screens():
            if screen.availableGeometry().intersects(rect):
                return x, y
        primary = gui_app.primaryScreen()
        if primary is None:
            log.warning(
                "[hud] clamp: target (%d,%d %dx%d) outside all screens "
                "and no primary — keeping it",
                x, y, self._current_width, self._current_height,
            )
            return x, y
        geom = primary.availableGeometry()
        fallback_x = geom.left() + HUD_EDGE_INSET
        fallback_y = geom.top() + HUD_EDGE_INSET
        log.warning(
            "[hud] clamp: target (%d,%d %dx%d) outside all screens — "
            "falling back to primary top-left (%d,%d)",
            x, y, self._current_width, self._current_height,
            fallback_x, fallback_y,
        )
        return fallback_x, fallback_y

    def _on_screen_config_changed(self, *_args) -> None:
        """Handle ``primaryScreenChanged`` / ``screenAdded`` / ``screenRemoved``.

        Re-probe the chosen-screen anchor and, if the HUD is currently
        visible, snap it to the new top-right. Catches docking /
        undocking, monitor unplug, and resolution-change mid-session
        without waiting for the next hide-show cycle.
        """
        log.info("[hud] screen configuration changed — re-anchoring")
        self._log_all_screens()
        self._refresh_screen_anchor()
        if not self._currently_visible:
            return
        self._anchor_right_x = self._screen_right_edge
        self._anchor_y = self._screen_top_edge
        x = self._anchor_right_x - self._current_width
        y = self._anchor_y
        x, y = self._clamp_to_visible_screen(x, y)
        self._suppress_anchor_update = True
        try:
            self.move(int(x), int(y))
        finally:
            self._suppress_anchor_update = False

    # ------------------------------------------------------------------
    # macOS overlay tweaks: NSStatusWindowLevel + collection behaviour
    # so the HUD floats above app windows, survives Spaces /
    # fullscreen, doesn't take focus.
    # ------------------------------------------------------------------

    def _log_lsappinfo_self(self) -> None:
        """Log ``lsappinfo info <self_pid>`` so we can see how macOS
        registered this HUD subprocess with WindowServer / LaunchServices.

        Look for ``bundleID="com.sayzo.agent"`` + a present ``cgsConnection``
        in the output — that's the kill-criterion for "HUD is realized."
        ``bundleID=[NULL]`` + ``!cgsConnection`` = realization failed,
        check the geometry / opacity init path.
        """
        import os
        import subprocess
        try:
            out = subprocess.run(
                ["lsappinfo", "info", str(os.getpid())],
                capture_output=True, text=True, timeout=2,
            ).stdout
            log.info("[hud] lsappinfo self (pid=%d):\n%s", os.getpid(), out)
        except Exception:
            log.warning("[hud] lsappinfo probe failed", exc_info=True)

    _OVERLAY_TWEAK_MAX_RETRIES = 3
    _OVERLAY_TWEAK_RETRY_MS = 500

    def _apply_mac_overlay_tweaks(self) -> None:
        try:
            from AppKit import (  # type: ignore[import-not-found]
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowCollectionBehaviorIgnoresCycle,
                NSWindowCollectionBehaviorTransient,
            )
            import objc  # type: ignore[import-not-found]
        except Exception:
            log.warning(
                "[hud] AppKit unavailable — mac overlay tweaks skipped",
                exc_info=True,
            )
            return

        # Hide the Dock icon for the HUD subprocess via the shared
        # helper that Settings + Setup already use.
        from sayzo_agent.gui.common.mac_dock import set_dock_visible
        set_dock_visible(False)

        NS_STATUS_WINDOW_LEVEL = 25

        # Qt's ``QWidget.winId()`` returns the native NSView pointer on
        # macOS — NOT the NSWindow. Bridge the view, then walk to its
        # host NSWindow via ``-[NSView window]``.
        try:
            ns_view = objc.objc_object(c_void_p=int(self.winId()))
        except Exception:
            log.warning("[hud] could not bridge QWidget winId to NSView", exc_info=True)
            return

        try:
            ns_window = ns_view.window()
        except Exception:
            log.warning("[hud] NSView.window() lookup failed", exc_info=True)
            return
        if ns_window is None:
            # With v3.3.0's opacity-based hiding the NSWindow should be
            # realized at loadFinished. If it isn't, the realization
            # might just be a tick behind Qt's loadFinished signal —
            # retry up to 3 times before giving up. Promoted to error
            # (was warning) because the opacity fix is supposed to
            # guarantee a realized NSWindow here.
            self._overlay_tweak_attempts += 1
            if self._overlay_tweak_attempts <= self._OVERLAY_TWEAK_MAX_RETRIES:
                log.warning(
                    "[hud] NSView has no NSWindow yet — retrying in %dms (attempt %d/%d)",
                    self._OVERLAY_TWEAK_RETRY_MS,
                    self._overlay_tweak_attempts,
                    self._OVERLAY_TWEAK_MAX_RETRIES,
                )
                QTimer.singleShot(
                    self._OVERLAY_TWEAK_RETRY_MS, self._apply_mac_overlay_tweaks,
                )
                return
            log.error(
                "[hud] NSView has no NSWindow after %d retries — overlay tweaks skipped "
                "(HUD will inherit default window level / hidesOnDeactivate=YES; "
                "expect HUD to disappear when agent loses focus)",
                self._OVERLAY_TWEAK_MAX_RETRIES,
            )
            return

        try:
            ns_window.setLevel_(NS_STATUS_WINDOW_LEVEL)
            log.info("[hud] setLevel_ ok (NSStatusWindowLevel=25)")
        except Exception:
            log.warning("[hud] setLevel_ failed", exc_info=True)

        try:
            behavior = (
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorTransient
                | NSWindowCollectionBehaviorIgnoresCycle
            )
            ns_window.setCollectionBehavior_(behavior)
            log.info("[hud] setCollectionBehavior_ ok")
        except Exception:
            log.warning("[hud] setCollectionBehavior_ failed", exc_info=True)

        try:
            # Without this, Qt's ``Qt.WindowType.Tool`` flag maps to an
            # NSPanel-style window with ``hidesOnDeactivate=YES`` by
            # default. The HUD subprocess never has focus (it's
            # LSUIElement-style, no Dock icon), so AppKit treats it as
            # "always inactive" and the window is hidden the moment any
            # other app is frontmost. Pinning this False keeps the HUD
            # visible regardless of which app currently has focus.
            ns_window.setHidesOnDeactivate_(False)
            log.info("[hud] setHidesOnDeactivate_(False) ok")
        except Exception:
            log.warning("[hud] setHidesOnDeactivate_ failed", exc_info=True)

        log.info("[hud] mac overlay tweaks applied")

    # ------------------------------------------------------------------
    # stdin command pipeline. The parent agent's launcher writes
    # newline-delimited JSON commands; we read them on a daemon thread
    # and forward into the React app via window.hudBridge.dispatch().
    # ------------------------------------------------------------------

    def _stdin_command_loop(self) -> None:
        try:
            for line in sys.stdin:
                raw = line.strip()
                if not raw:
                    continue
                if raw.lower() == "quit":
                    self._dispatch_quit()
                    return
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("[hud] stdin: malformed JSON: %r", raw[:200])
                    continue
                cmd = payload.get("cmd") if isinstance(payload, dict) else None
                if cmd == "quit":
                    self._dispatch_quit()
                    return
                # Forward via the Qt signal so the JS dispatch happens
                # on the GUI thread (QWebEngineView is GUI-thread-only).
                self._command_received.emit(raw)
        except Exception:
            log.warning("[hud] stdin reader crashed", exc_info=True)
        log.info("[hud] stdin closed — quitting")
        self._dispatch_quit()

    def _dispatch_command_on_gui_thread(self, raw_json: str) -> None:
        """Slot connected to ``_command_received`` — runs on GUI thread."""
        if not self._loaded_event.is_set():
            with self._pending_lock:
                self._pending_commands.append(raw_json)
                excess = len(self._pending_commands) - self._pending_commands_cap
                if excess > 0:
                    del self._pending_commands[:excess]
            return
        self._evaluate_js_dispatch(raw_json)

    def _flush_pending_commands(self) -> None:
        with self._pending_lock:
            pending = list(self._pending_commands)
            self._pending_commands.clear()
        for raw in pending:
            self._evaluate_js_dispatch(raw)

    def _evaluate_js_dispatch(self, raw_json: str) -> None:
        # Embed via JSON.parse to avoid quote-escape juggling. The
        # React side's bridge exposes ``window.hudBridge.dispatch(cmd)``.
        escaped = raw_json.replace("\\", "\\\\").replace("`", "\\`")
        js = (
            "(function(){"
            "try{"
            f"const payload = JSON.parse(`{escaped}`);"
            "if (window.hudBridge && typeof window.hudBridge.dispatch === 'function') {"
            "  window.hudBridge.dispatch(payload);"
            "}"
            "}catch(e){console.warn('hud dispatch err', e);}"
            "})();"
        )
        try:
            self._view.page().runJavaScript(js)
        except Exception:
            log.warning("[hud] runJavaScript dispatch failed", exc_info=True)

    def _dispatch_quit(self) -> None:
        self._quitting = True
        # Schedule app quit on the GUI thread.
        QApplication.instance().quit()

    def start_stdin_reader(self) -> None:
        reader = threading.Thread(
            target=self._stdin_command_loop,
            name="sayzo-hud-stdin",
            daemon=True,
        )
        reader.start()


class HudWindow:
    """Public façade that preserves the pre-v2.11 ``run_blocking()`` interface.

    Used by:
    * ``scripts/preview_hud.py``'s ``_run_demo`` (calls
      ``HudWindow(cfg, demo=True).run_blocking()``).
    * ``sayzo_agent/__main__.py::hud`` (the ``sayzo-agent hud`` CLI).

    Internally creates / reuses a ``QApplication``, constructs the
    Qt host widget, starts the stdin reader, and enters the event
    loop.
    """

    def __init__(self, cfg: Config, *, demo: bool = False) -> None:
        self._cfg = cfg
        self._demo = demo

    def run_blocking(self) -> None:
        # PySide6 requires a QApplication before any QWidget. Reuse an
        # existing one if the host process already has one (e.g.
        # ``preview_hud.py launcher`` mode runs in the same process for
        # tests).
        app = QApplication.instance() or QApplication(sys.argv)
        widget = _HudHostWidget(self._cfg, demo=self._demo)
        widget.show()
        widget.start_stdin_reader()
        _install_sigint_handler(app)
        # Wire Qt-level OS-shutdown hooks before app.exec() so they're
        # active for the entire lifetime of the event loop. The handler
        # uses a view_provider closure so the aboutToQuit teardown
        # reads the LATEST view reference at fire time (not whatever
        # was current at install time). See gui/hud/shutdown_hooks.py
        # for the full rationale (v2.16.0 plan).
        from sayzo_agent.gui.hud.shutdown_hooks import install_qt_shutdown_hooks

        install_qt_shutdown_hooks(app, view_provider=lambda: widget._view)
        exit_code = app.exec()
        log.info("[hud] Qt event loop exited rc=%s", exit_code)


# ----------------------------------------------------------------------
# Module-level helpers.
# ----------------------------------------------------------------------


def _install_sigint_handler(app: QApplication) -> None:
    """Make Ctrl+C exit the Qt event loop cleanly.

    Qt's ``QApplication.exec()`` doesn't return control to Python often
    enough for the default SIGINT handler to fire, so ``Ctrl+C`` is
    swallowed by the running event loop. We install our own handler
    that calls ``app.quit()`` AND register a low-priority QTimer that
    fires every 200 ms — the timer's callback returns control to
    Python, which lets the pending SIGINT actually be delivered. This
    is the canonical Qt-app pattern for honouring Ctrl+C.

    Only meaningful when stdin is a TTY (i.e. someone running
    ``scripts/preview_hud.py demo``). The agent's spawned HUD
    subprocess has no TTY and the parent uses the ``quit`` stdin
    command for shutdown, which works regardless.
    """
    def _handler(signum: int, _frame) -> None:  # noqa: ANN001
        log.info("[hud] SIGINT received — quitting Qt event loop")
        app.quit()

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        # signal.signal can only be called from the main thread; the
        # HUD subprocess always satisfies this, but be defensive.
        return
    # Tickle the Python interpreter periodically so the C-level signal
    # is actually delivered. Reuse the timer across re-entrant
    # ``run_blocking`` calls so we don't orphan QTimer objects when
    # the same QApplication serves multiple HUD windows in one process
    # (e.g. ``preview_hud.py`` test sequences).
    if getattr(app, "_sayzo_sigint_timer", None) is None:
        keep_alive_timer = QTimer()
        keep_alive_timer.timeout.connect(lambda: None)
        keep_alive_timer.start(200)
        setattr(app, "_sayzo_sigint_timer", keep_alive_timer)
