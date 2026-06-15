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
import os
import signal
import sys
import threading
import time
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
from sayzo_agent.gui.hud.js_escape import build_dispatch_js

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

# A second QtWebEngine renderer death within this window is treated as a
# persistent failure: instead of reloading again we exit so the parent
# launcher's respawn ladder takes over with a fresh process.
_RENDER_DEATH_WINDOW_SECS = 30.0

# If the React app never emits ``hud_ready`` within this long after
# ``loadFinished`` (transport handshake wedged, JS bundle broken), the
# subprocess exits so the parent respawns it rather than leaving a
# frozen-but-alive ghost window. Normal boots set ready in <2 s.
_READY_WATCHDOG_SECS = 60.0

# Child exit codes that signal the parent launcher to respawn (any
# non-zero exit trips the reader-loop EOF → ladder; these are documented
# so agent.log triage can tell apart the recovery reasons).
_EXIT_RENDERER_DOUBLE_DEATH = 3
_EXIT_READY_WATCHDOG = 4


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
    # Emitted by the stdin reader thread when a ``ping`` command arrives;
    # the connected slot runs on the GUI thread and replies ``pong``, so
    # a successful pong proves the Qt event loop (not just the process)
    # is alive. Renderer-only death is covered separately by
    # renderProcessTerminated.
    _ping_received = Signal(str)

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
        # Running count of resizeEvents fired by Qt's platform plugin —
        # diagnostic for the 1-px shrink-restore trick in
        # :meth:`_set_window_visible`. The trick calls ``setGeometry``
        # twice with different widths in immediate succession to force
        # a paintEvent → UpdateLayeredWindow refresh; whether Qt
        # delivers two real resize events or coalesces them has been
        # an unverified assumption since v3.x. Logged on every visible
        # show so we can finally measure it.
        self._n_resize_events = 0
        # Bounded retry counter for the macOS overlay-tweak path —
        # NSWindow realization can lag Qt's loadFinished signal by a
        # tick under load. See :meth:`_apply_mac_overlay_tweaks`.
        self._overlay_tweak_attempts = 0
        # Renderer-death recovery bookkeeping (see
        # :meth:`_on_render_process_terminated`).
        self._last_render_death = 0.0
        # Win32 EVENT_SYSTEM_FOREGROUND hook handle + its ctypes
        # callback. The callback MUST stay referenced on self or it is
        # garbage-collected and the C side calls into freed memory.
        self._win_event_hook = None
        self._win_event_proc = None

        # Frameless, top-most, no taskbar/Alt-Tab, no focus theft.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # Genuine per-pixel alpha — pixels the React app paints as
        # transparent become OS-level transparent. This is what gives us
        # "invisible when there's no content" without needing any
        # opacity / show / hide manipulation: when React's hasContent
        # is false it renders an empty page, every pixel is alpha=0,
        # and the user sees nothing. When content arrives, React
        # renders the card and those pixels become opaque.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Realize the native window at the real top-right anchor from
        # boot so macOS WindowServer establishes a CGS connection for
        # the subprocess immediately. Pre-v3.3.0 we set geometry to
        # (-20000, 0) which left the NSWindow unrealized on macOS
        # (WindowServer doesn't allocate a backing surface for a
        # transparent frameless widget shown outside every screen) —
        # that's why the agent-spawned HUD never appeared even though
        # ``sayzo-agent hud --demo`` did. v3.3.0 fixed that with
        # opacity-init, but `setWindowOpacity` on a `WA_TranslucentBackground`
        # window collides with QtWebEngine's compositor on macOS and
        # the window stayed invisible even after opacity went to 1.0.
        # v3.3.1: realize on-screen at full opacity and rely on
        # per-pixel alpha for the empty-state invisibility.
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
        # Recover from a QtWebEngine renderer (GPU/render-process) crash:
        # without this the host QWidget stays alive but blank forever and
        # the parent only sees process-exit, not a dead renderer.
        self._view.page().renderProcessTerminated.connect(
            self._on_render_process_terminated
        )
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
        # Queued connection (reader thread → GUI thread) so the pong is
        # written from the Qt loop, proving it's alive.
        self._ping_received.connect(self._on_ping)

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
            "[hud] opening Qt window at (x=%s y=%s); right edge=%s top edge=%s "
            "initial w=%s h=%s demo=%s url=%s (invisibility via per-pixel alpha)",
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
        # Deferred-show landing point. Chromium has loaded the React bundle
        # and the page background is already painted transparent
        # (QWebEngineView.page().setBackgroundColor(QColor(0,0,0,0)) above).
        # Realizing the native window now means the user never sees an empty
        # Qt backing buffer composited briefly before Chromium's first frame
        # — that's the boot-flicker we used to ship. macOS WindowServer
        # establishes its CGS connection here (same path as v3.3.1's at-boot
        # realization, just delayed by load latency); orderFrontRegardless
        # in _set_window_visible later handles the LSUIElement-parent case.
        # MUST run before _set_click_through(True) below — that call's Win32
        # branch uses self.winId(), which returns 0 until the native window
        # is realized.
        if not self.isVisible():
            log.info("[hud] deferred-show: realizing window after loadFinished")
            self.show()
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
        # Boot defaults to click-through ON — the HUD starts with no
        # React content, so its top-right footprint must not steal
        # clicks from the user's app underneath. The first
        # ``_set_window_visible(True)`` from React will flip it off.
        self._set_click_through(True)
        self._loaded_event.set()
        # Drain any commands that landed before the page was ready.
        self._flush_pending_commands()
        # Win32: re-assert topmost whenever the foreground window changes
        # (a meeting going borderless-fullscreen would otherwise occlude
        # us). Installed from the Qt main thread, which pumps Windows
        # messages — the documented requirement for OUTOFCONTEXT hooks.
        if sys.platform == "win32":
            self._install_foreground_hook_win()
        # Arm the ready watchdog: if React never handshakes (transport
        # wedged / broken bundle), exit so the parent respawns rather
        # than leaving a frozen window. Skipped in demo/preview (which
        # may have no parent to respawn it).
        if not self._demo:
            QTimer.singleShot(
                int(_READY_WATCHDOG_SECS * 1000), self._check_ready_watchdog
            )

    # ------------------------------------------------------------------
    # Visibility — driven entirely by React's per-pixel alpha. When
    # React has no content, every pixel is transparent and the user
    # sees nothing. When content arrives, those pixels become opaque.
    # This method's only job is anchor management: snap to top-right
    # at the start of a new visible session, reset anchor on hide so
    # the next cycle starts fresh. No opacity / show / hide
    # manipulation — those caused the v3.3.0 invisibility regression
    # on macOS (setWindowOpacity collides with the WA_TranslucentBackground
    # + QtWebEngine compositor path).
    # ------------------------------------------------------------------

    def _set_window_visible(self, visible: bool) -> None:
        # Redundant HIDE calls are a no-op (window already hidden).
        # Redundant SHOW calls — i.e. setWindowVisible(True) while
        # already visible — DO run the paint-refresh sequence again
        # (1-px setGeometry trick + update() + OS-state log) so the
        # layered-window backing store recomposes for a swapped card
        # at the same dimensions (supersede + activeCard.request_id
        # transitions in HudApp.tsx). But the anchor reset on a real
        # hidden→visible transition (refresh_screen_anchor + snap to
        # top-right) MUST NOT fire on a redundant SHOW — that would
        # silently destroy the user's mid-session drag captured by
        # moveEvent. Pre-v3.11.0 the early-return covered both
        # branches and the user-drag preservation was implicit; this
        # method now gates the anchor reset explicitly on
        # ``was_visible``.
        if not visible and not self._currently_visible:
            return
        was_visible = self._currently_visible
        self._currently_visible = visible
        if visible:
            if not was_visible:
                # Real hidden→visible transition: refresh the chosen-
                # screen anchor and snap to top-right. The value
                # captured at HUD-subprocess __init__ is suspect in
                # two common scenarios — agent auto-starts at boot
                # before Windows finishes painting the desktop, and
                # the user reconfigures monitors between captures —
                # so we re-probe Qt's screen API here. The user-drag
                # override in ``moveEvent`` only persists within a
                # single visible session; going hidden and back
                # resets it intentionally.
                self._refresh_screen_anchor()
                self._anchor_right_x = self._screen_right_edge
                self._anchor_y = self._screen_top_edge
            # Use whatever anchor is current — preserved across
            # redundant SHOWs so a card swap doesn't yank the
            # window away from where the user dragged it.
            x = self._anchor_right_x - self._current_width
            y = self._anchor_y
            # Last-resort safety net: if for some reason the target
            # rect lies outside every detected screen (screen was
            # removed between refresh and move, fallback values are
            # stale, …), clamp to a primary-screen position the user
            # can definitely see.
            x, y = self._clamp_to_visible_screen(x, y)
            # Force a real Qt geometry change to drive a paintEvent →
            # UpdateLayeredWindow refresh of the OS layered surface.
            # WA_TranslucentBackground + WS_EX_LAYERED on Windows means
            # the on-screen pixels come from UpdateLayeredWindow, which
            # is itself driven by a top-level paintEvent. self.move()
            # at unchanged position is a no-op, and setGeometry with
            # identical values is skipped by most platform plugins.
            # Without a real change, every upstream cache short-circuits
            # (React lastW/lastH in HudApp.tsx, Python _current_* in
            # _set_window_size below, Qt move-to-same-position here)
            # and consecutive same-size toasts silently fail to paint:
            # the OS layered surface stays stuck on the previous
            # (alpha=0 empty) frame and the user sees nothing.
            #
            # 1-px width shrink-then-restore forces a real resize event
            # → paintEvent → UpdateLayeredWindow → fresh composite of
            # whatever QWebEngineView's GPU surface holds (the
            # just-mounted card content). The 1-px change lives for one
            # event-loop tick and is sub-perceptible at typical card
            # widths (>300 px).
            w = self._current_width
            h = self._current_height
            resize_count_before = self._n_resize_events
            self._suppress_anchor_update = True
            try:
                self.setGeometry(int(x), int(y), max(1, w - 1), h)
                self.setGeometry(int(x), int(y), w, h)
            finally:
                self._suppress_anchor_update = False
            # Belt-and-suspenders alongside the 1-px trick above:
            # explicit ``update()`` posts a paintEvent into Qt's event
            # queue even if the platform plugin coalesces the two
            # setGeometry calls into a single (no-net-change) resize.
            # ``_view.update()`` mirrors that on the QWebEngineView
            # widget so the WebEngine compositor's own surface gets
            # composed into the host pixmap before
            # ``UpdateLayeredWindow`` is called. Neither replaces the
            # React-side double-rAF gate in HudApp.tsx, which is the
            # load-bearing fix for the paint-stall race; these just
            # close the residual window where Qt's compose runs but
            # finds a stale GPU surface.
            self.update()
            if self._view is not None:
                self._view.update()
            resize_count_delta = self._n_resize_events - resize_count_before
            log.info(
                "[hud] window visibility → shown (pos=%d,%d size=%dx%d resize_events=%d)",
                int(x), int(y), self._current_width, self._current_height,
                resize_count_delta,
            )
            # macOS: force the NSWindow into the visible Z-stack via
            # ``orderFrontRegardless``. Required when the parent process
            # is an LSUIElement app (the production Sayzo agent): the
            # HUD subprocess gets a real CGS connection (lsappinfo
            # confirms ``bundleID="com.sayzo.agent"`` + own ASN) but
            # WindowServer doesn't compose the window into the visible
            # output — exactly the v3.3.0–v3.3.2 symptom. Demo from
            # Terminal works because Terminal is a regular Application,
            # not LSUIElement, and WindowServer composes those children
            # automatically. ``orderFrontRegardless`` (Apple's documented
            # API for this exact case) moves the window to the front of
            # its level even when the owning app isn't active.
            if sys.platform == "darwin":
                self._force_order_front_mac()
            elif sys.platform == "win32":
                # Re-claim the front of the top-most band. WS_EX_TOPMOST is
                # set once at construction (Qt.WindowStaysOnTopHint) but is
                # NOT re-asserted, so a borderless-fullscreen meeting window
                # raised later sits ABOVE us and the toast renders behind it
                # — invisible to the user while IsWindowVisible still reports
                # True. Win32 analog of _force_order_front_mac above.
                self._force_topmost_win()
            # Window is now content-bearing — let it receive clicks so
            # the user can interact with the card / pill / actionable.
            self._set_click_through(False)
            # On Windows, ask the OS what it actually thinks the window
            # is doing. Diagnostic for the layered-window paint-stall:
            # if ``IsWindowVisible`` is True and the rect matches what
            # we set but the user still reports nothing on screen, the
            # failure is definitively in the pixmap-content path
            # (UpdateLayeredWindow composing a stale WebEngine GPU
            # surface), not the window-state path. Skipped on macOS —
            # the failure-mode there is different (LSUIElement-parent
            # WindowServer composition, handled by
            # ``_force_order_front_mac`` above).
            if sys.platform == "win32":
                self._log_win_os_state()
        else:
            # Going hidden: leave geometry in place — the actual visual
            # disappearance is React rendering all-transparent pixels.
            # Reset the anchor back to the screen's top-right so the
            # NEXT visibility cycle starts fresh at the top-right
            # corner. Without this the window would re-appear wherever
            # the user dragged it last session, which on a "no content
            # → new content" transition feels like a stale state from
            # the previous arm cycle.
            self._anchor_right_x = self._screen_right_edge
            self._anchor_y = self._screen_top_edge
            log.info(
                "[hud] window visibility → hidden (per-pixel alpha will paint transparent)",
            )
            # Window is now content-empty — let clicks pass through to
            # whatever app is underneath. Without this the HUD's
            # geometry (which persists at the top-right via the
            # v3.3.x always-on-screen design) creates a dead-click
            # zone there. Pre-v3.3.0 the offscreen-move pattern hid
            # the window from click-hit-testing entirely; the
            # opacity / per-pixel-alpha approach we use now needs an
            # explicit OS-level toggle.
            self._set_click_through(True)

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
            "[hud] window size → %dx%d at (%d,%d) (visible=%s)",
            self._current_width, self._current_height,
            int(x), int(y), self._currently_visible,
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

    def resizeEvent(self, event) -> None:  # noqa: ANN001 — Qt signature
        """Bump the resize-event diagnostic counter, then defer to Qt.

        Lets :meth:`_set_window_visible` report how many real resize
        events the 1-px ``setGeometry`` shrink-restore trick actually
        produced. If the count is consistently 0 or 1, Qt's platform
        plugin is coalescing the trick into a no-op — explains why
        the paint-stall workaround was unreliable enough to warrant
        the React-side double-rAF gate.
        """
        super().resizeEvent(event)
        self._n_resize_events += 1

    def _log_win_os_state(self) -> None:
        """Log Win32's view of the host window state after a show.

        Diagnostic-only — answers the "is the window actually visible
        according to Windows?" question that Qt's ``isVisible()`` can't
        (it returns ``True`` even when the window is occluded or stuck
        on a stale layered surface). Uses lazy import so the macOS
        startup path doesn't pay the cost.
        """
        try:
            import win32gui  # type: ignore
        except Exception:
            log.debug("[hud] win32gui not importable — skipping OS-state log")
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            log.warning("[hud] winId() not available for OS-state log", exc_info=True)
            return
        try:
            is_visible = bool(win32gui.IsWindowVisible(hwnd))
            is_iconic = bool(win32gui.IsIconic(hwnd))
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            log.warning("[hud] win32gui OS-state query failed", exc_info=True)
            return
        log.info(
            "[hud] OS state after show: hwnd=%d IsWindowVisible=%s "
            "IsIconic=%s GetWindowRect=%r",
            hwnd, is_visible, is_iconic, rect,
        )
        # Occlusion probe — the line that answers "did the user actually
        # SEE it?" IsWindowVisible above is True even when a later top-most
        # window (borderless-fullscreen Meet/Zoom) is painted over us, which
        # is the exact way a toast goes unseen on Windows (and stays
        # invisible to every other diagnostic). Ask Win32 which top-level
        # window owns the pixel at our centre; if it isn't us, we were
        # covered. Runs after _set_click_through(False) cleared
        # WS_EX_TRANSPARENT, so hit-testing reaches our window when we are
        # genuinely on top.
        try:
            left, top, right, bottom = rect
            # Sample a point that reliably lands on RENDERED content, NOT the
            # geometric centre. This is a per-pixel-alpha layered window
            # (WA_TranslucentBackground + WS_EX_LAYERED), and Windows hit-tests
            # layered windows by alpha: fully-transparent pixels (alpha=0) are
            # click-through, so WindowFromPoint there returns whatever is
            # BEHIND us — a false "occluded". The pill / first toast / card
            # always anchors at the TOP of the shell, so a point ~24px below
            # the top edge, horizontally centred, sits on opaque content for
            # every overlay state.
            cx = (left + right) // 2
            cy = min(top + 24, bottom - 1)
            pt_hwnd = win32gui.WindowFromPoint((cx, cy))
            # GA_ROOT = 2 → top-level owner of whatever child is at the point
            # (WindowFromPoint can return our QtWebEngine child HWND).
            owner = win32gui.GetAncestor(pt_hwnd, 2) if pt_hwnd else 0
            occluded = bool(owner) and owner != hwnd
            owner_title = win32gui.GetWindowText(owner) if owner else ""
            log.info(
                "[hud] occlusion probe: sample=(%d,%d) owner_hwnd=%s "
                "owner_title=%r occluded_by_other_window=%s",
                cx, cy, owner, owner_title, occluded,
            )
        except Exception:
            log.debug("[hud] occlusion probe failed", exc_info=True)

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

    def _set_click_through(self, ignore: bool) -> None:
        """Toggle OS-level click-through on the host window.

        With v3.3.x's always-on-screen HUD design (window holds its
        position even when React's content is empty), the HUD would
        otherwise create a dead-click zone at the top-right corner of
        the screen — clicks land on the Qt window and never reach the
        app underneath. Toggle this OFF when there's content to
        interact with (consent card, pill) and ON when the HUD is
        idle (React renders nothing). CSS ``pointer-events: none`` on
        the HudShell isn't enough on its own — that only affects
        QtWebEngine's CSS hit-testing, not the OS-level click
        intercept on the host window.

        macOS uses ``NSWindow.setIgnoresMouseEvents:`` and Windows
        uses ``WS_EX_TRANSPARENT``; both are the canonical
        platform APIs for window-level mouse pass-through.
        """
        if sys.platform == "darwin":
            self._set_click_through_mac(ignore)
        elif sys.platform == "win32":
            self._set_click_through_win(ignore)

    def _set_click_through_mac(self, ignore: bool) -> None:
        try:
            import objc  # type: ignore[import-not-found]
        except Exception:
            log.warning("[hud] objc unavailable — click-through toggle skipped", exc_info=True)
            return
        try:
            ns_view = objc.objc_object(c_void_p=int(self.winId()))
            ns_window = ns_view.window()
        except Exception:
            log.warning("[hud] click-through: NSView/NSWindow lookup failed", exc_info=True)
            return
        if ns_window is None:
            log.debug("[hud] click-through: NSWindow not realized yet — skip")
            return
        try:
            ns_window.setIgnoresMouseEvents_(bool(ignore))
            log.info("[hud] setIgnoresMouseEvents_(%s) ok", ignore)
        except Exception:
            log.warning("[hud] setIgnoresMouseEvents_ failed", exc_info=True)

    def _set_click_through_win(self, ignore: bool) -> None:
        try:
            import ctypes
        except Exception:
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        WS_EX_LAYERED = 0x80000
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ignore:
                # WS_EX_TRANSPARENT requires WS_EX_LAYERED on a window
                # that doesn't already have it. WA_TranslucentBackground
                # usually sets LAYERED, but assert it to be safe.
                new_style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
            else:
                new_style = style & ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            log.info("[hud] WS_EX_TRANSPARENT %s ok", "set" if ignore else "cleared")
        except Exception:
            log.warning("[hud] WS_EX_TRANSPARENT toggle failed", exc_info=True)

    def _force_topmost_win(self) -> None:
        """Re-assert ``HWND_TOPMOST`` on the host window (Windows).

        Win32 analog of :meth:`_force_order_front_mac`. The window gets
        ``WS_EX_TOPMOST`` once at construction (``Qt.WindowStaysOnTopHint``),
        but that is set-once and does NOT re-raise us above OTHER top-most
        windows activated later — most importantly a borderless-fullscreen
        meeting window (Chrome/Meet, Zoom). Among top-most windows z-order
        is "most recently raised wins", so a toast that fires after the user
        has gone fullscreen renders BEHIND the meeting and is never seen
        (``IsWindowVisible`` still reports True — occlusion is invisible to
        that check; see the occlusion probe in :meth:`_log_win_os_state`).

        Routed through **pywin32** (``win32gui.SetWindowPos``), NOT raw
        ``ctypes.windll.user32.SetWindowPos``. The raw-ctypes path shipped
        through v3.18.0 passed ``HWND_TOPMOST = -1`` with no ``argtypes``; on
        64-bit Python that ``-1`` is zero-extended to ``0x00000000FFFFFFFF``
        instead of the sign-extended ``(HWND)-1 = 0xFFFFFFFFFFFFFFFF`` sentinel
        Windows expects, so EVERY call failed with ``GetLastError() == 1400``
        (``ERROR_INVALID_WINDOW_HANDLE``) and the toast stayed buried behind
        the meeting — logged blandly as ``ok=False`` with no error code, which
        is why it went undiagnosed across v3.12–v3.18.0. pywin32 marshals the
        handle via ``PyHANDLE`` and gets ``(HWND)-1`` right. Confirmed by live
        probe on the same hwnd: raw ctypes → errno 1400, pywin32 → success.

        ``SWP_NOACTIVATE`` keeps us from stealing focus from the meeting app
        (same contract as the rest of the HUD's no-focus-theft design). The
        ``HWND_NOTOPMOST`` → ``HWND_TOPMOST`` toggle forces a real
        re-insertion at the front of the top-most band: a bare
        ``HWND_TOPMOST`` on an already-top-most window can be optimized to a
        no-op that won't jump above another *recently-raised* top-most window
        (e.g. a Chrome OAuth popup — the exact case the user hit).
        """
        try:
            import pywintypes  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]
        except Exception:
            log.warning(
                "[hud] pywin32 unavailable — topmost re-assert skipped",
                exc_info=True,
            )
            return
        flags = (
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        )
        try:
            hwnd = int(self.winId())
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            log.info("[hud] re-asserted HWND_TOPMOST ok")
        except pywintypes.error as e:
            # Self-diagnosing: name the real Win32 error instead of the blind
            # ok=False that hid errno 1400 (ERROR_INVALID_WINDOW_HANDLE) for
            # three releases.
            log.warning(
                "[hud] HWND_TOPMOST re-assert failed winerror=%s (%s)",
                getattr(e, "winerror", "?"), getattr(e, "strerror", e),
            )
        except Exception:
            log.warning("[hud] HWND_TOPMOST re-assert failed", exc_info=True)

    def _force_order_front_mac(self) -> None:
        """Force the HUD's NSWindow into the visible Z-stack.

        Workaround for the agent-spawned-HUD-invisible bug that
        survived v3.3.0–v3.3.2: WindowServer registers the subprocess
        correctly (lsappinfo shows valid ASN + bundleID) but doesn't
        compose the window into visible output when the parent process
        is itself LSUIElement (the production Sayzo agent). Calling
        ``orderFrontRegardless`` on the NSWindow forces it to the
        front of its level even when the owning app isn't active —
        Apple's documented API for this exact case.

        Safe to call even if the NSWindow isn't realized yet (early
        return with a debug log). Call on every visibility-shown
        transition; order can shift over time as other apps' windows
        move around.
        """
        try:
            import objc  # type: ignore[import-not-found]
        except Exception:
            log.warning("[hud] objc unavailable — orderFrontRegardless skipped", exc_info=True)
            return
        try:
            ns_view = objc.objc_object(c_void_p=int(self.winId()))
            ns_window = ns_view.window()
        except Exception:
            log.warning("[hud] orderFrontRegardless: NSView/NSWindow lookup failed", exc_info=True)
            return
        if ns_window is None:
            log.debug("[hud] orderFrontRegardless: NSWindow not realized yet — skip")
            return
        try:
            ns_window.orderFrontRegardless()
            log.info("[hud] orderFrontRegardless ok")
        except Exception:
            log.warning("[hud] orderFrontRegardless failed", exc_info=True)

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

        try:
            # Qt.WindowType.Tool maps to NSPanel on macOS, and NSWindow.hasShadow
            # defaults to YES for every NSWindow subclass (Apple ref:
            # developer.apple.com/documentation/appkit/nswindow/1419117-hasshadow).
            # WindowServer draws that shadow around the FRAME rect, not the
            # opaque-pixel mask — so a frameless transparent host whose React
            # content is fully alpha=0 still gets a faint shadow / border drawn
            # at the boot-time geometry (100x100, top-right). This shadow is
            # the "phantom transparent box" users see at top-right when the
            # HUD has no content. Suppress it.
            ns_window.setHasShadow_(False)
            log.info("[hud] setHasShadow_(False) ok")
        except Exception:
            log.warning("[hud] setHasShadow_ failed", exc_info=True)

        try:
            # WA_TranslucentBackground should already set this to NO on macOS,
            # but assert it — belt-and-suspenders against a future Qt regression
            # where Tool windows quietly default back to opaque. Opaque + zero-
            # alpha content would render solid black, which is far worse than
            # the current "faint box" — keep the invariant explicit.
            ns_window.setOpaque_(False)
            log.info("[hud] setOpaque_(False) ok")
        except Exception:
            log.warning("[hud] setOpaque_ failed", exc_info=True)

        log.info("[hud] mac overlay tweaks applied")

        # One-shot orderFrontRegardless at boot so the NSWindow is in
        # the visible Z-stack from the start. Without this, the first
        # content arrival might paint to a backing surface that
        # WindowServer hasn't composed into the visible output yet
        # (the exact agent-spawned-HUD-invisible bug from v3.3.0–v3.3.2).
        # Subsequent visibility-show transitions also call this — see
        # ``_set_window_visible``.
        self._force_order_front_mac()

    # ------------------------------------------------------------------
    # Liveness + recovery.
    # ------------------------------------------------------------------

    def _on_ping(self, ping_id: str) -> None:
        """Reply ``pong`` to the parent's heartbeat (runs on the GUI thread)."""
        try:
            self._bridge.emit_event({"event": "pong", "id": ping_id})
        except Exception:
            log.warning("[hud] pong reply failed", exc_info=True)

    def _on_render_process_terminated(self, status, exit_code) -> None:  # noqa: ANN001
        """Recover from a QtWebEngine renderer (GPU/render-process) crash.

        First death in a 30 s window: reload the page (deferred to the
        next event-loop tick — reloading inside the signal handler is
        undefined). Re-loading re-fires ``loadFinished`` → re-wires the
        bridge and React re-emits ``hud_ready``, at which point the
        parent replays the active pill. A second death within the window
        means reload isn't helping, so exit and let the parent's respawn
        ladder bring up a fresh process.
        """
        now = time.monotonic()
        log.error(
            "[hud] renderProcessTerminated: status=%s exitCode=%s", status, exit_code
        )
        if now - self._last_render_death < _RENDER_DEATH_WINDOW_SECS:
            log.error(
                "[hud] renderer died twice within %.0fs — exiting %d for parent respawn",
                _RENDER_DEATH_WINDOW_SECS, _EXIT_RENDERER_DOUBLE_DEATH,
            )
            os._exit(_EXIT_RENDERER_DOUBLE_DEATH)
        self._last_render_death = now
        log.warning("[hud] reloading web view after renderer death")
        QTimer.singleShot(0, self._view.reload)

    def _check_ready_watchdog(self) -> None:
        """Exit if React never emitted ``hud_ready`` (handshake wedged)."""
        if self._bridge.ready_event.is_set():
            return
        log.error(
            "[hud] React never emitted hud_ready within %.0fs of loadFinished — "
            "exiting %d for parent respawn",
            _READY_WATCHDOG_SECS, _EXIT_READY_WATCHDOG,
        )
        os._exit(_EXIT_READY_WATCHDOG)

    def _install_foreground_hook_win(self) -> None:
        """SetWinEventHook(EVENT_SYSTEM_FOREGROUND) → re-assert topmost.

        WS_EX_TOPMOST is set once at construction and does NOT re-raise
        us above OTHER top-most windows activated later (a borderless-
        fullscreen Meet/Zoom raised after a toast appears). Reacting to
        every foreground change keeps the HUD on top without polling.
        See learn.microsoft.com SetWinEventHook.
        """
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        if self._win_event_hook is not None:
            return
        EVENT_SYSTEM_FOREGROUND = 0x0003
        WINEVENT_OUTOFCONTEXT = 0x0000
        WinEventProcType = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,  # hWinEventHook
            wintypes.DWORD,   # event
            wintypes.HWND,    # hwnd
            wintypes.LONG,    # idObject
            wintypes.LONG,    # idChild
            wintypes.DWORD,   # dwEventThread
            wintypes.DWORD,   # dwmsEventTime
        )

        def _cb(hHook, event, hwnd, idObject, idChild, thread, ts):  # noqa: ANN001
            try:
                if not self._currently_visible:
                    return
                if int(hwnd) == int(self.winId()):
                    return
                self._force_topmost_win()
            except Exception:
                pass

        try:
            user32 = ctypes.windll.user32
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD,    # eventMin
                wintypes.DWORD,    # eventMax
                wintypes.HMODULE,  # hmodWinEventProc
                WinEventProcType,  # pfnWinEventProc
                wintypes.DWORD,    # idProcess
                wintypes.DWORD,    # idThread
                wintypes.DWORD,    # dwFlags
            ]
            # HWINEVENTHOOK is a HANDLE (pointer-width). Without an explicit
            # restype, ctypes defaults to c_int and truncates the 64-bit
            # handle, so the value stored in self._win_event_hook can't be
            # unhooked cleanly on quit (same class of bug as the HWND_TOPMOST
            # marshalling above, just on the return path).
            user32.SetWinEventHook.restype = wintypes.HANDLE
            self._win_event_proc = WinEventProcType(_cb)  # keep ref alive
            self._win_event_hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                0, self._win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT,
            )
            log.info(
                "[hud] foreground WinEvent hook installed ok=%s",
                bool(self._win_event_hook),
            )
        except Exception:
            log.warning("[hud] SetWinEventHook failed", exc_info=True)
            self._win_event_hook = None
            self._win_event_proc = None

    def _uninstall_foreground_hook_win(self) -> None:
        if self._win_event_hook is None:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            # Match the HANDLE restype set on SetWinEventHook so the full
            # 64-bit hook handle round-trips instead of a truncated c_int.
            user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            user32.UnhookWinEvent.restype = wintypes.BOOL
            user32.UnhookWinEvent(self._win_event_hook)
        except Exception:
            pass
        self._win_event_hook = None
        self._win_event_proc = None

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
                if cmd == "ping":
                    # Liveness ping — reply pong from the GUI thread,
                    # never forward to React (proving the Qt loop, not
                    # the renderer, is alive).
                    ping_id = payload.get("id", "") if isinstance(payload, dict) else ""
                    self._ping_received.emit(str(ping_id))
                    continue
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
        # Embed as a json.dumps'd double-quoted string literal + JSON.parse
        # on the JS side — see js_escape.build_dispatch_js for why the old
        # template-literal embedding was an injection vector.
        try:
            self._view.page().runJavaScript(build_dispatch_js(raw_json))
        except Exception:
            log.warning("[hud] runJavaScript dispatch failed", exc_info=True)

    def _dispatch_quit(self) -> None:
        self._quitting = True
        if sys.platform == "win32":
            self._uninstall_foreground_hook_win()
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
        # Defer widget.show() to _on_load_finished (in _HudHostWidget).
        # Realizing the native window before Chromium has composited the
        # transparent React content used to flash an empty 100x100 backing
        # buffer at the top-right corner — see _on_load_finished for the
        # full rationale. Geometry was set on-screen in __init__, so the
        # deferred show still lands at the correct top-right anchor.
        #
        # Safety net: if loadFinished never arrives (corrupted dist,
        # Chromium init hang, …), force-show after 8 s so the HUD is
        # never permanently invisible. 8 s sits inside the launcher's
        # 15 s wait_for_ready tolerance and covers slow cold-boot
        # Chromium init.
        def _fail_show_if_not_visible() -> None:
            if not widget.isVisible():
                log.info(
                    "[hud] fail-show timer fired: loadFinished hasn't arrived in "
                    "8s; showing widget anyway as a safety net (benign on slow "
                    "cold boots — loadFinished, when it lands, re-runs the real "
                    "callback wiring)",
                )
                widget.show()

        QTimer.singleShot(8000, _fail_show_if_not_visible)
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
