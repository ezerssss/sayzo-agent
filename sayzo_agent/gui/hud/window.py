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

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor
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
# ``HudBridge.set_window_size``, so the user never sees this. The
# window is created offscreen anyway.
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
        # Right-edge anchor (screen width minus inset). Computed once;
        # used as the INITIAL anchor when the HUD first comes onscreen.
        self._screen_right_edge = _compute_screen_right_edge()
        # Live "where the window's top-right corner currently sits"
        # anchor. Updated by ``moveEvent`` whenever the window moves
        # while visible (programmatic or user-initiated drag). On
        # resize (collapse pill → dot, expand dot → pill, toast added)
        # we pin the right edge to this and let the left edge slide,
        # so the window grows / shrinks toward the left rather than
        # snapping back to the screen's top-right corner if the user
        # has dragged it elsewhere.
        self._anchor_right_x = self._screen_right_edge
        self._anchor_y = HUD_EDGE_INSET
        # Set while we're doing a programmatic move / resize, so
        # ``moveEvent`` only treats user-initiated drags as anchor
        # updates. Without this guard, programmatic ``setGeometry``
        # calls would clobber the anchor with stale geometry values
        # if Qt fires ``moveEvent`` before ``resizeEvent``.
        self._suppress_anchor_update = False

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
        # Start at the offscreen anchor so we don't briefly flash the
        # window during boot. React's ResizeObserver and
        # ``set_window_visible(true)`` move + resize us when there's
        # content.
        offscreen_x, offscreen_y = _offscreen_anchor()
        self.setGeometry(
            offscreen_x, offscreen_y, INITIAL_HUD_WIDTH, INITIAL_HUD_HEIGHT,
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

        # Load the React bundle.
        index = webui_index_path()
        if not index.exists():
            log.error("[hud] UI assets missing at %s — HUD will not render", index)
            return
        url = _hud_url(index, demo=self._demo)
        log.info(
            "[hud] opening Qt window offscreen at (x=%s y=%s); right edge=%s "
            "initial w=%s h=%s demo=%s url=%s",
            offscreen_x, offscreen_y, self._screen_right_edge,
            INITIAL_HUD_WIDTH, INITIAL_HUD_HEIGHT, self._demo, url,
        )
        self._view.load(QUrl(url))

    @property
    def bridge(self) -> HudBridge:
        return self._bridge

    # ------------------------------------------------------------------
    # Lifecycle hooks.
    # ------------------------------------------------------------------

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            log.warning("[hud] page load reported not-ok")
        log.info("[hud] page loaded — wiring bridge callbacks")
        # Apply macOS-specific overlay tweaks (status-window-level,
        # collection behaviour, hides-on-deactivate=False) once Qt has
        # realised the native NSWindow.
        if sys.platform == "darwin":
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
    # Visibility — toggle the window between onscreen (top-right) and
    # offscreen (-20000, 0) using setGeometry. No focus stealing
    # because the window has Qt.WindowDoesNotAcceptFocus.
    # ------------------------------------------------------------------

    def _set_window_visible(self, visible: bool) -> None:
        if visible == self._currently_visible:
            return
        self._currently_visible = visible
        if visible:
            # Use the live anchor: top-right corner by default, or
            # wherever the user dragged the window last cycle.
            x = self._anchor_right_x - self._current_width
            y = self._anchor_y
        else:
            # Going hidden — reset the anchor back to the screen's
            # top-right so the NEXT visibility cycle (new pill /
            # card / toast after the previous batch fully cleared)
            # starts fresh at the top-right corner. Without this the
            # window would re-appear wherever the user dragged it
            # last session, which on a "no content → new content"
            # transition feels like a stale state from the previous
            # arm cycle.
            self._anchor_right_x = self._screen_right_edge
            self._anchor_y = HUD_EDGE_INSET
            x, y = _offscreen_anchor()
        self._suppress_anchor_update = True
        try:
            self.move(int(x), int(y))
        finally:
            self._suppress_anchor_update = False
        log.info("[hud] window visibility → %s", "shown" if visible else "hidden")

    def _set_window_size(self, width: int, height: int) -> None:
        if width == self._current_width and height == self._current_height:
            return
        self._current_width = int(width)
        self._current_height = int(height)
        if self._currently_visible:
            # Pin the RIGHT edge to the live anchor so the window
            # grows / shrinks toward the LEFT. Collapse (pill → dot)
            # shrinks toward the right edge of the user's current
            # window position instead of snapping back to the screen
            # corner; expand (dot → pill) grows leftward from the
            # dot's current right edge instead of overflowing off
            # the right of the monitor.
            x = self._anchor_right_x - self._current_width
            y = self._anchor_y
        else:
            x, y = _offscreen_anchor()
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
    # macOS overlay tweaks: NSStatusWindowLevel + collection behaviour
    # so the HUD floats above app windows, survives Spaces /
    # fullscreen, doesn't take focus.
    # ------------------------------------------------------------------

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

        try:
            ns_window = objc.objc_object(c_void_p=int(self.winId()))
        except Exception:
            log.warning("[hud] could not bridge QWidget winId to NSWindow", exc_info=True)
            return

        try:
            ns_window.setLevel_(NS_STATUS_WINDOW_LEVEL)
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
        except Exception:
            log.warning("[hud] setCollectionBehavior_ failed", exc_info=True)

        try:
            ns_window.setHidesOnDeactivate_(False)
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


def _offscreen_anchor() -> tuple[int, int]:
    """A point far enough off the primary monitor to be invisible."""
    return (-20000, 0)


def _compute_screen_right_edge() -> int:
    """Pixel x-coordinate of "right edge minus inset" on the primary monitor.

    Used as the right anchor for the HUD: regardless of the current
    window width, the top-right corner snaps to this column. Falls back
    to a ctypes / Cocoa probe / sensible default.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            return max(0, screen_w - HUD_EDGE_INSET)
        except Exception:
            log.warning("[hud] win screen probe failed", exc_info=True)
    if sys.platform == "darwin":
        try:
            from AppKit import NSScreen  # type: ignore[import-not-found]

            main = NSScreen.mainScreen()
            if main is not None:
                frame = main.frame()
                screen_w = int(frame.size.width)
                return max(0, screen_w - HUD_EDGE_INSET)
        except Exception:
            log.warning("[hud] mac screen probe failed", exc_info=True)
    return 1280


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
