"""Pywebview-hosted Settings window.

Spawned as a subprocess via ``sayzo-agent settings`` so each window owns its
own main thread. Mirrors :mod:`sayzo_agent.gui.setup.window` for asset path
resolution and pywebview wiring; the only structural differences are the
window size, the URL's ``?route=settings`` query param, and the lack of a
return value (Settings has no completion / quit semantics — closing the
window simply returns control to the subprocess shell).

Idle mode (``--idle`` on the CLI) keeps a Settings subprocess pre-warmed at
agent boot. The window starts hidden, the X-button is intercepted to call
``hide()`` instead of ``destroy()``, and a stdin reader thread accepts
newline-delimited commands (``show`` / ``hide`` / ``quit``) from the parent
agent. Trade-off: ~50–100 MB resident, but the user-perceived "open Settings"
latency drops from 1–3 s (subprocess spawn + WebView2 init) to the time it
takes to render a hidden window — effectively instant.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import webview

from sayzo_agent.config import Config
from sayzo_agent.gui.common.assets import icon_path, webui_index_path
from sayzo_agent.gui.settings.bridge import Bridge

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo — Settings"
WINDOW_SIZE = (920, 640)
WINDOW_MIN_SIZE = (760, 520)


def _settings_url(index: Path, *, pane: Optional[str]) -> str:
    """Build a ``file://…/index.html#route=settings[&pane=…]`` URL.

    Hash fragments (``#``), not query strings (``?``), are used to avoid the
    pywebview / WebView2 file-URL quirk where the query suffix gets folded
    into the file path lookup ("file not found: index.html?route=settings").
    Fragments are preserved by every browser engine for ``file://`` URLs and
    are equally readable from React via ``window.location.hash``.
    """
    base = index.as_uri()
    params = {"route": "settings"}
    if pane:
        params["pane"] = pane
    return f"{base}#{urlencode(params)}"


class SettingsWindow:
    """Owns the pywebview window + bridge for the Settings flow."""

    def __init__(
        self,
        cfg: Config,
        *,
        pane: Optional[str] = None,
        idle: bool = False,
    ) -> None:
        self._cfg = cfg
        self._pane = pane
        self._idle = idle
        self._bridge = Bridge(cfg, initial_pane=pane)
        # In idle mode the X button hides; only an explicit ``quit`` command
        # (or the GUI loop ending naturally) tears the window down. The flag
        # is flipped by ``_handle_quit_command`` so the second on_closing
        # invocation lets pywebview destroy normally.
        self._quitting = False

    def run_blocking(self) -> None:
        index = webui_index_path()
        if not index.exists():
            log.error(
                "[settings] UI assets missing at %s — skipping window", index
            )
            return

        url = _settings_url(index, pane=self._pane)
        log.info(
            "[settings] opening window at %s (idle=%s)", url, self._idle,
        )

        window = webview.create_window(
            title=WINDOW_TITLE,
            url=url,
            js_api=self._bridge,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE,
            resizable=True,
            background_color="#FFFFFF",
            text_select=False,
            hidden=self._idle,
        )
        self._bridge._attach_window(window)

        icon_arg: dict = {}
        icon = icon_path()
        if icon is not None:
            icon_arg["icon"] = str(icon)

        if self._idle:
            self._wire_idle(window)

        # webview.start() blocks until the last window is destroyed. In
        # idle mode that only happens on the ``quit`` command (or stdin EOF
        # / parent death), since the X button hides instead of destroying.
        webview.start(debug=self._cfg.debug, **icon_arg)
        log.info("[settings] window closed")

    # ------------------------------------------------------------------
    # Idle-mode wiring: hide-on-close + stdin command reader.
    # ------------------------------------------------------------------

    def _wire_idle(self, window: "webview.Window") -> None:
        # X-button → hide instead of destroy. Returning False cancels the
        # close so pywebview leaves the OS window alive (just hidden).
        # ``_quitting`` is set by the explicit ``quit`` command; in that
        # case we let the close go through.
        def on_closing() -> Optional[bool]:
            if self._quitting:
                return None
            try:
                window.hide()
            except Exception:
                log.warning("[settings] hide on close failed", exc_info=True)
            return False

        window.events.closing += on_closing

        # Stdin reader runs in a daemon thread so webview.start() can own
        # the main thread. Commands queue inside the pipe before the GUI is
        # mapped — pywebview's window.show() is safe to call as soon as the
        # event loop is running, which is by the time webview.start()
        # returns its first iteration.
        reader = threading.Thread(
            target=self._stdin_command_loop,
            name="sayzo-settings-stdin",
            args=(window,),
            daemon=True,
        )
        reader.start()

    def _stdin_command_loop(self, window: "webview.Window") -> None:
        """Read newline-delimited commands from stdin and drive the window.

        Recognised commands: ``show``, ``hide``, ``quit``. EOF (parent
        agent died or closed our stdin pipe) is treated as ``quit`` —
        without this fallback, an agent crash would leave an orphan
        Settings process resident with no way for the user to dismiss it.
        """
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if not cmd:
                    continue
                if cmd == "show":
                    self._dispatch_show(window)
                elif cmd == "hide":
                    self._dispatch_hide(window)
                elif cmd == "quit":
                    self._dispatch_quit(window)
                    return
                else:
                    log.warning("[settings] unknown stdin command: %r", cmd)
        except Exception:
            log.warning("[settings] stdin reader crashed", exc_info=True)
        # EOF: parent agent vanished. Tear down so we don't orphan.
        log.info("[settings] stdin closed — quitting")
        self._dispatch_quit(window)

    @staticmethod
    def _dispatch_show(window: "webview.Window") -> None:
        try:
            window.show()
        except Exception:
            log.warning("[settings] show failed", exc_info=True)

    @staticmethod
    def _dispatch_hide(window: "webview.Window") -> None:
        try:
            window.hide()
        except Exception:
            log.warning("[settings] hide failed", exc_info=True)

    def _dispatch_quit(self, window: "webview.Window") -> None:
        self._quitting = True
        try:
            window.destroy()
        except Exception:
            log.warning("[settings] destroy failed", exc_info=True)
