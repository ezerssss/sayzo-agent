"""Pywebview-hosted first-run setup window.

Run via :meth:`SetupWindow.run_blocking` from the service main thread —
``webview.start`` blocks until the window is destroyed (by the user clicking
finish / cancel, or by closing the window). When it returns, the bridge's
``result`` field tells the caller whether to continue the service startup
or exit.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import webview

from sayzo_agent.config import Config
from sayzo_agent.gui.setup.bridge import Bridge, SetupResult

log = logging.getLogger(__name__)

WINDOW_TITLE = "Sayzo Agent — Setup"
WINDOW_SIZE = (720, 560)
WINDOW_MIN_SIZE = (640, 480)


def _webui_index_path() -> Path:
    """Resolve the path to ``index.html`` in dev and frozen builds.

    Frozen: ``<sys._MEIPASS>/sayzo_agent/gui/webui/dist/index.html``
    Dev:    ``<repo>/sayzo_agent/gui/webui/dist/index.html``
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "sayzo_agent" / "gui" / "webui" / "dist"  # type: ignore[attr-defined]
    else:
        # __file__ is .../sayzo_agent/gui/setup/window.py — climb to gui/.
        base = Path(__file__).resolve().parent.parent / "webui" / "dist"
    return base / "index.html"


class SetupWindow:
    """Owns the pywebview window + bridge for the first-run flow."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._bridge = Bridge(cfg)

    def run_blocking(self) -> SetupResult:
        index = _webui_index_path()
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
        webview.start(debug=self._cfg.debug)

        log.info("setup window closed: result=%s", self._bridge.result.value)
        return self._bridge.result
