"""Pywebview-hosted first-run setup window.

Run via :meth:`SetupWindow.run_blocking` from the service main thread —
``webview.start`` blocks until the window is destroyed (by the user clicking
finish / cancel, or by closing the window). When it returns, the bridge's
``result`` field tells the caller whether to continue the service startup
or exit.
"""
from __future__ import annotations

import logging
import os

import webview

from sayzo_agent.config import Config
from sayzo_agent.gui.common.assets import icon_path, webui_index_path
from sayzo_agent.gui.common.pywebview_patches import (
    patch_clear_user_data_none_guard,
    patch_on_close_swallow_teardown,
)
from sayzo_agent.gui.common.win_shutdown import install_shutdown_protection
from sayzo_agent.gui.setup.bridge import Bridge, SetupResult

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo — Setup"
# Height sized for the tallest step — the recording-indicator picker, whose
# full-width 16:9 hero preview + selectors + caption need ~340px of content
# room below the header/title. 680 keeps that step scroll-free while still
# fitting comfortably on a 768px-tall display (680 + title bar + taskbar
# ≈ 750). The other (text-light) steps just gain breathing room. Resizable,
# so a user on a short screen can shrink it; the picker scrolls if they do.
WINDOW_SIZE = (720, 680)
WINDOW_MIN_SIZE = (640, 480)


class SetupWindow:
    """Owns the pywebview window + bridge for the first-run flow."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._bridge = Bridge(cfg)

    def run_blocking(self) -> SetupResult:
        index = webui_index_path()
        if not index.exists():
            log.error(
                "first-run UI assets missing at %s — skipping setup window", index
            )
            # Treat as QUIT so the service exits cleanly rather than starting
            # in a broken-setup state.
            return SetupResult.QUIT

        url = index.as_uri()
        log.info("opening setup window at %s", url)

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
        )
        self._bridge._attach_window(window)

        # Windows-only, all must precede webview.start(); see
        # gui/common/pywebview_patches.py + win_shutdown.py for the
        # rationale (zesty-zooming-taco.md plan).
        patch_clear_user_data_none_guard()
        patch_on_close_swallow_teardown()

        # Windows-only: intercept SystemEvents.SessionEnding so we exit
        # cleanly via WM_QUIT before pywebview's FormClosed handler runs
        # against a dying WebView2 child process and surfaces a JIT
        # dialog that blocks Windows shutdown. See gui/common/win_shutdown.py.
        # Setup has no idle-vs-quit distinction, so no set_quitting callback.
        install_shutdown_protection(window)

        # Catch the user-closed-via-X path (the Cancel button is handled by
        # Bridge.quit_app, which hard-exits directly). pywebview's Cocoa
        # backend calls NSApplication.stop_() in windowWillClose_; Apple's
        # docs say stop: only takes effect after the next NSEvent is
        # received, so without further user interaction the NSApp runloop
        # sits idle and webview.start() never returns. We hard-exit here if
        # the bridge result is still QUIT (default — user never reached
        # Done) so the process can't get wedged.
        def _on_closed() -> None:
            if self._bridge.result == SetupResult.QUIT:
                log.warning("setup window closed via X — exiting")
                os._exit(0)

        window.events.closed += _on_closed

        # webview.start() blocks until the window is destroyed. debug=True
        # opens the devtools panel — gated on Config.debug for ad-hoc UI work.
        # ``icon`` sets the taskbar/dock icon so the installer window shows
        # Sayzo in dev previews (in production the NSIS-installed shortcut
        # provides the icon via its AUMID).
        #
        # ``private_mode=False`` short-circuits pywebview's
        # ``EdgeChrome.clear_user_data`` at shutdown — same rationale as
        # Settings; OAuth flows through the system browser via PKCE, not
        # the in-webview cookie jar. See gui/settings/window.py for the
        # full explanation.
        icon_arg: dict = {}
        icon = icon_path()
        if icon is not None:
            icon_arg["icon"] = str(icon)
        webview.start(debug=self._cfg.debug, private_mode=False, **icon_arg)

        log.info("setup window closed: result=%s", self._bridge.result.value)
        return self._bridge.result
