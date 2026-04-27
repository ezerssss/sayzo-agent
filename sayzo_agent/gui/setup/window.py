"""Pywebview-hosted first-run setup window.

Run via :meth:`SetupWindow.run_blocking` from the service main thread —
``webview.start`` blocks until the window is destroyed (by the user clicking
finish / cancel, or by closing the window). When it returns, the bridge's
``result`` field tells the caller whether to continue the service startup
or exit.
"""
from __future__ import annotations

import logging

import webview

from sayzo_agent.config import Config
from sayzo_agent.gui.common.assets import icon_path, webui_index_path
from sayzo_agent.gui.setup.bridge import Bridge, SetupResult

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo — Setup"
WINDOW_SIZE = (720, 560)
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

        # webview.start() blocks until the window is destroyed. debug=True
        # opens the devtools panel — gated on Config.debug for ad-hoc UI work.
        # ``icon`` sets the taskbar/dock icon so the installer window shows
        # Sayzo in dev previews (in production the NSIS-installed shortcut
        # provides the icon via its AUMID).
        icon_arg: dict = {}
        icon = icon_path()
        if icon is not None:
            icon_arg["icon"] = str(icon)
        webview.start(debug=self._cfg.debug, **icon_arg)

        log.info("setup window closed: result=%s", self._bridge.result.value)
        return self._bridge.result
