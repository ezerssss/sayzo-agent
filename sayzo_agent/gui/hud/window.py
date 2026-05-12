"""HUD pywebview window — hosted in the ``sayzo-agent hud --idle`` subprocess.

A single frameless, transparent, always-on-top window pinned to the top-
right of the primary monitor. The React app inside (see
``gui/webui/src/HudApp.tsx``) handles all visual state — pill, dot,
consent card, toast, actionable. Python's only job here is to:

1. Host the window with the right platform flags so it doesn't steal
   focus from the user's meeting app.
2. Read newline-delimited JSON commands from stdin and forward them
   into the webview via ``window.evaluate_js("window.hudBridge.dispatch(...)")``.
3. Exit cleanly on ``quit`` or stdin EOF.

Focus-stealing is the highest-risk regression and the reason we
deliberately do not call ``activateIgnoringOtherApps_`` / ``SetForegroundWindow``
anywhere. On macOS we install a Cocoa-level override that prevents the
window from ever becoming "key" (input focus), so showing the HUD never
takes the user out of their meeting app. On Windows the ``WS_EX_NOACTIVATE``
extended style serves the same purpose. The minimum-viable defence works
without those tweaks — the window is just unstyled and may briefly steal
focus on appear; with the tweaks it stays out of the way entirely.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import webview

from sayzo_agent.config import Config
from sayzo_agent.gui.common.assets import webui_index_path
from sayzo_agent.gui.common.safe_quit import safe_quit_window
from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection
from sayzo_agent.gui.hud.bridge import HudBridge

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo HUD"

# A fixed transparent canvas. React draws inside it via HudShell's
# ``fixed inset-0`` layout, anchored to the top-right corner. Sized
# generously so the pill + 3 stacked toasts + a card + demo control
# strip all fit without dynamic resizing (we deliberately avoid
# pywebview's ``Window.resize`` — it's quirky on Windows and a transparent
# canvas with click-through has no visual cost for the extra real-estate).
HUD_WIDTH = 420
HUD_HEIGHT = 640

# Distance from the screen edge to the HUD's anchor corner. Small enough
# that the pill feels glued to the top-right, large enough to not clip
# under the Windows accessibility border / macOS rounded screen corners.
HUD_EDGE_INSET = 8


def _hud_url(index: Path, *, demo: bool) -> str:
    """Build a ``file://…/index.html#route=hud[&demo=1]`` URL.

    Same hash-fragment convention as Settings — query strings get
    mangled by WebView2 under file://.
    """
    base = index.as_uri()
    params = {"route": "hud"}
    if demo:
        params["demo"] = "1"
    return f"{base}#{urlencode(params)}"


class HudWindow:
    """Owns the pywebview HUD window for the subprocess lifetime."""

    def __init__(self, cfg: Config, *, demo: bool = False) -> None:
        self._cfg = cfg
        self._demo = demo
        self._bridge = HudBridge()
        # Filled in run_blocking. Used by the stdin reader to forward
        # commands via evaluate_js once the window is realised.
        self._window: Optional["webview.Window"] = None
        self._loaded_event = threading.Event()
        # Buffer for commands that arrive over stdin before the window
        # is loaded. Flushed in order once ``loaded`` fires.
        self._pending_commands: list[str] = []
        self._pending_lock = threading.Lock()
        # Flipped by the stdin reader when it sees ``quit``. Distinguishes
        # explicit teardown from a transient hide.
        self._quitting = False

    def run_blocking(self) -> None:
        index = webui_index_path()
        if not index.exists():
            log.error(
                "[hud] UI assets missing at %s — HUD will not start", index
            )
            return

        url = _hud_url(index, demo=self._demo)
        # Top-right anchor of the primary monitor. We probe through
        # pywebview if possible (5.x exposes ``webview.screens``);
        # otherwise default to (0, 0) and let the user drag. Both
        # platforms position windows relative to the top-left of the
        # primary monitor in pywebview's coordinate space.
        x, y = _compute_top_right_anchor()
        log.info(
            "[hud] opening window at %s (x=%s y=%s w=%s h=%s demo=%s)",
            url, x, y, HUD_WIDTH, HUD_HEIGHT, self._demo,
        )

        # Per-platform transparency choice.
        #
        # On macOS we want a genuinely see-through canvas so empty
        # regions are click-through to the meeting app. The Cocoa
        # NSWindow APIs (setOpaque_/setBackgroundColor_) handle this
        # cleanly — applied in _apply_mac_overlay_tweaks.
        #
        # On Windows, pywebview's transparent=True path uses a WinForms
        # TransparencyKey (chromakey at RGB(255,0,0)). WebView2's GPU
        # compositing doesn't route mouse events correctly through a
        # layered host with a chromakey: clicks land on the chromakey-
        # pass-through pixels but get lost on the painted UI inside.
        # We accept a visible solid-colored rectangle on Windows as a
        # consequence — same as how Loom's recorder bar and most
        # Electron-based overlay HUDs render on Windows. The visible
        # rectangle is sized minimally (no big transparent canvas)
        # so the chrome footprint is small.
        want_transparent = sys.platform == "darwin"

        self._window = webview.create_window(
            title=WINDOW_TITLE,
            url=url,
            js_api=self._bridge,
            width=HUD_WIDTH,
            height=HUD_HEIGHT,
            x=x,
            y=y,
            frameless=True,
            resizable=False,
            on_top=True,
            transparent=want_transparent,
            # Sayzo theme: light surface, dark ink (matches Settings + setup
            # windows). On Windows where transparency is off, the user sees a
            # white card; on macOS the BackColor is overridden to clear in
            # _apply_mac_overlay_tweaks so transparency takes over.
            background_color="#FFFFFF",
            text_select=False,
            # The HUD is always shown — there's no "idle pre-warm hidden"
            # phase like Settings. The launcher gates visibility via
            # ``hide_pill`` / ``hide_all`` commands; the React app just
            # renders nothing in the hidden state.
            hidden=False,
        )

        def _mark_quitting() -> None:
            self._quitting = True

        # Wire load + close hooks.
        def on_closing() -> Optional[bool]:
            # The HUD has no user-facing close button (frameless window).
            # If something else triggers a close (shutdown, kill), let
            # it through.
            return None

        def on_loaded() -> None:
            log.info("[hud] WebView2 loaded — applying platform tweaks")
            self._apply_platform_tweaks()
            # Install the Windows shutdown handler AFTER the window has
            # loaded — by then pywebview has initialized pythonnet and
            # the Microsoft.Win32 / System.Windows.Forms namespaces are
            # resolvable. Doing this pre-load (as Settings still does)
            # logs harmless ImportError warnings about pythonnet not
            # being ready yet.
            try:
                assert self._window is not None
                install_shutdown_protection(self._window, set_quitting=_mark_quitting)
            except Exception:
                log.warning("[hud] install_shutdown_protection failed", exc_info=True)
            self._loaded_event.set()
            # Drain any commands that landed before the React app was
            # ready. After this, the stdin reader will forward inline.
            self._flush_pending_commands()

        self._window.events.closing += on_closing
        self._window.events.loaded += on_loaded

        # Stdin reader runs in a daemon thread so webview.start() owns
        # the main thread (required by Cocoa + pywebview).
        reader = threading.Thread(
            target=self._stdin_command_loop,
            name="sayzo-hud-stdin",
            daemon=True,
        )
        reader.start()

        webview.start(debug=self._cfg.debug)
        log.info("[hud] window closed — subprocess exiting")

    # ------------------------------------------------------------------
    # Platform-specific tweaks: make the HUD an overlay that doesn't
    # steal focus from the user's foreground app.
    # ------------------------------------------------------------------

    def _apply_platform_tweaks(self) -> None:
        if sys.platform == "darwin":
            self._apply_mac_overlay_tweaks()
        elif sys.platform == "win32":
            self._apply_win_overlay_tweaks()

    def _apply_mac_overlay_tweaks(self) -> None:
        """Float above app windows + survive Spaces / fullscreen + don't take focus.

        Three calls in sequence:

        * ``setLevel_(NSStatusWindowLevel)`` — keeps the HUD above
          normal windows but below the menu bar and modal alerts.
        * ``setCollectionBehavior_(...)`` — joins the union of every
          space (so the user switching desktops doesn't lose the
          HUD), survives fullscreen Zoom/Meet via
          ``FullScreenAuxiliary``, and stays out of window-cycle UIs.
        * ``setHidesOnDeactivate_(False)`` — defaults to True on
          ``Accessory`` policy apps; we want the HUD visible while the
          user is in another app, which is the entire point.

        We also drop the app's activation policy to ``Accessory`` so
        the subprocess doesn't add a Dock icon. ``canBecomeKeyWindow``
        isn't overridden directly here — the combination above plus
        never calling ``activateIgnoringOtherApps_`` keeps focus on the
        user's foreground app in practice. If focus-theft reports come
        in, the next step is an Objective-C category override of
        ``canBecomeKeyWindow``.
        """
        try:
            from AppKit import (  # type: ignore[import-not-found]
                NSApp,
                NSApplicationActivationPolicyAccessory,
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowCollectionBehaviorIgnoresCycle,
                NSWindowCollectionBehaviorTransient,
            )
        except Exception:
            log.warning(
                "[hud] AppKit unavailable — cannot apply mac overlay tweaks",
                exc_info=True,
            )
            return

        # NSStatusWindowLevel = 25. We hardcode rather than rely on a
        # pyobjc constant because the constant name has changed across
        # macOS versions and pyobjc minor releases.
        NS_STATUS_WINDOW_LEVEL = 25

        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            log.warning(
                "[hud] setActivationPolicy_(Accessory) failed",
                exc_info=True,
            )

        try:
            ns_window = self._cocoa_window()
        except Exception:
            log.warning(
                "[hud] cocoa window introspection failed",
                exc_info=True,
            )
            return
        if ns_window is None:
            log.warning("[hud] no NSWindow handle found — overlay tweaks skipped")
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

        # Opt out of the standard NSWindow "first responder takes focus"
        # behaviour. ``setAcceptsMouseMovedEvents_`` and
        # ``setMovableByWindowBackground_`` both reduce the chance the
        # window pulls focus when the user mouses through it.
        try:
            ns_window.setMovableByWindowBackground_(True)
        except Exception:
            pass

        # The window is borderless — sometimes pywebview's Cocoa backend
        # forgets to mark it as opaque-or-not. Forcing opaque=False makes
        # sure the transparent canvas under the React content actually
        # composites against the desktop.
        try:
            ns_window.setOpaque_(False)
            from AppKit import NSColor  # type: ignore[import-not-found]

            ns_window.setBackgroundColor_(NSColor.clearColor())
        except Exception:
            log.warning("[hud] setOpaque_/clearColor failed", exc_info=True)

        log.info("[hud] mac overlay tweaks applied")

    def _cocoa_window(self):  # type: ignore[no-untyped-def]
        """Best-effort NSWindow lookup for the current pywebview window."""
        # pywebview's cocoa backend stashes the NSWindow on its BrowserView
        # singleton. The exact attribute name has been ``window`` since 5.0;
        # we look it up by import path so a pywebview internal rename doesn't
        # silently break this module.
        try:
            from webview.platforms import cocoa  # type: ignore[import-not-found]
        except Exception:
            return None
        bv_instances = getattr(cocoa.BrowserView, "instances", None)
        if not bv_instances:
            return None
        assert self._window is not None
        bv = bv_instances.get(self._window.uid)
        if bv is None:
            return None
        # ``bv.window`` is the NSWindow. Fall back to ``bv.webkit.window()``
        # if that ever moves.
        for attr in ("window", "_window"):
            ns = getattr(bv, attr, None)
            if ns is not None:
                return ns
        webkit = getattr(bv, "webkit", None)
        if webkit is not None:
            return webkit.window()
        return None

    def _apply_win_overlay_tweaks(self) -> None:
        """Mark the HUD as a topmost tool window above normal Z-order.

        ``WS_EX_TOOLWINDOW`` removes the HUD from Alt+Tab — it's a
        floating overlay, not an app window. ``WS_EX_TOPMOST`` is set
        by pywebview's ``on_top=True`` already; we re-assert it to be
        sure since chained ``SetWindowLong`` calls otherwise drop
        unrelated extended styles.

        We deliberately do NOT set ``WS_EX_NOACTIVATE``. The flag would
        prevent the HUD from stealing focus when shown, but it also
        prevents WebView2 from routing mouse clicks to the embedded
        content (WebView2's input pipeline requires the host window's
        activation state to be valid). Clicking a consent prompt or
        the stop button would silently no-op. Trade-off accepted:
        clicking the HUD briefly takes focus (same behaviour as
        Granola); merely *showing* the HUD does not take focus,
        because we never call SetForegroundWindow on it.
        """
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            log.warning("[hud] ctypes unavailable — win tweaks skipped")
            return

        # win32 constants — hardcoded to avoid the pywin32 dependency
        # in this hot import path.
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_TOPMOST = 0x00000008

        try:
            hwnd = self._win_hwnd()
        except Exception:
            log.warning("[hud] win hwnd lookup failed", exc_info=True)
            return
        if hwnd == 0:
            log.warning("[hud] win hwnd is 0 — overlay tweaks skipped")
            return

        user32 = ctypes.windll.user32
        # Some Windows versions need the wide variants for 64-bit
        # pointers; GetWindowLongPtrW falls back to GetWindowLongW on
        # 32-bit builds.
        try:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
        except AttributeError:
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW

        try:
            current = get_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
        except Exception:
            log.warning("[hud] GetWindowLong failed", exc_info=True)
            return

        new_style = current | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        try:
            set_long(wintypes.HWND(hwnd), GWL_EXSTYLE, new_style)
            log.info(
                "[hud] win overlay tweaks applied (exstyle: 0x%X → 0x%X)",
                current, new_style,
            )
        except Exception:
            log.warning("[hud] SetWindowLong failed", exc_info=True)

    def _win_hwnd(self) -> int:
        """Return the HWND for the HUD window, or 0 if unknown."""
        try:
            from webview.platforms.winforms import BrowserView  # type: ignore[import-not-found]
        except Exception:
            return 0
        assert self._window is not None
        bv = BrowserView.instances.get(self._window.uid)
        if bv is None:
            return 0
        try:
            return int(bv.Handle.ToInt64())
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Stdin command pipeline.
    # ------------------------------------------------------------------

    def _stdin_command_loop(self) -> None:
        """Forward parent commands into the webview.

        Commands are newline-delimited JSON. ``quit`` is also accepted
        as a bare string for symmetry with the Settings subprocess
        contract (and so a panicked parent can kill us with a single
        ``echo quit > stdin``). EOF tears down the subprocess.
        """
        try:
            for line in sys.stdin:
                raw = line.strip()
                if not raw:
                    continue
                if raw.lower() == "quit":
                    self._dispatch_quit()
                    return
                # Parse as JSON. Malformed lines are logged and dropped.
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("[hud] stdin: malformed JSON: %r", raw[:200])
                    continue
                cmd = payload.get("cmd") if isinstance(payload, dict) else None
                if cmd == "quit":
                    self._dispatch_quit()
                    return
                self._forward_command(raw)
        except Exception:
            log.warning("[hud] stdin reader crashed", exc_info=True)
        log.info("[hud] stdin closed — quitting")
        self._dispatch_quit()

    def _forward_command(self, raw_json: str) -> None:
        """Push a JSON command into the React app's bridge.

        Commands that arrive before the React app is mounted are
        buffered; otherwise we ``evaluate_js`` immediately.
        """
        # evaluate_js needs a JS string expression. We embed the JSON
        # blob as a parsed object via ``JSON.parse`` so any quoting in
        # the payload doesn't need escape juggling on the Python side.
        if not self._loaded_event.is_set():
            with self._pending_lock:
                self._pending_commands.append(raw_json)
            return
        self._evaluate_js_dispatch(raw_json)

    def _flush_pending_commands(self) -> None:
        with self._pending_lock:
            pending = list(self._pending_commands)
            self._pending_commands.clear()
        for raw in pending:
            self._evaluate_js_dispatch(raw)

    def _evaluate_js_dispatch(self, raw_json: str) -> None:
        if self._window is None:
            return
        # Use a JS literal expression with JSON.parse so we don't have
        # to worry about quote escaping in the Python string.
        try:
            # Escape any backticks and backslashes for the template literal.
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
            self._window.evaluate_js(js)
        except Exception:
            log.warning("[hud] evaluate_js dispatch failed", exc_info=True)

    def _dispatch_quit(self) -> None:
        self._quitting = True
        if self._window is None:
            return
        try:
            safe_quit_window(self._window)
        except Exception:
            log.warning("[hud] safe_quit_window failed", exc_info=True)


def _compute_top_right_anchor() -> tuple[int, int]:
    """Best-effort top-right corner of the primary monitor.

    pywebview 5.x exposes ``webview.screens`` after the GUI loop starts
    — but we need a position *before* ``create_window``. Fall back to a
    Windows ctypes probe / macOS Cocoa probe / sensible default.
    """
    width = HUD_WIDTH
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            return max(0, screen_w - width - HUD_EDGE_INSET), HUD_EDGE_INSET
        except Exception:
            log.warning("[hud] win screen probe failed", exc_info=True)
    if sys.platform == "darwin":
        try:
            from AppKit import NSScreen  # type: ignore[import-not-found]

            main = NSScreen.mainScreen()
            if main is not None:
                frame = main.frame()
                screen_w = int(frame.size.width)
                return max(0, screen_w - width - HUD_EDGE_INSET), HUD_EDGE_INSET
        except Exception:
            log.warning("[hud] mac screen probe failed", exc_info=True)
    return 1280, HUD_EDGE_INSET
