"""Pywebview-hosted Settings window.

Spawned as a subprocess via ``sayzo-agent settings`` so each window owns its
own main thread. Mirrors :mod:`sayzo_agent.gui.setup.window` for asset path
resolution and pywebview wiring; the only structural differences are the
window size, the URL's ``?route=settings`` query param, and the lack of a
return value (Settings has no completion / quit semantics — closing the
window simply returns control to the subprocess shell).
"""
from __future__ import annotations

import logging
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
    ) -> None:
        self._cfg = cfg
        self._pane = pane
        self._bridge = Bridge(cfg, initial_pane=pane)

    def run_blocking(self) -> None:
        index = webui_index_path()
        if not index.exists():
            log.error(
                "[settings] UI assets missing at %s — skipping window", index
            )
            return

        url = _settings_url(index, pane=self._pane)
        log.info("[settings] opening window at %s", url)

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

        icon_arg: dict = {}
        icon = icon_path()
        if icon is not None:
            icon_arg["icon"] = str(icon)

        # webview.start() blocks until the window is destroyed (close button
        # or bridge.finish() call). debug=True opens the devtools panel —
        # gated on Config.debug for ad-hoc UI work. Exits the subprocess
        # cleanly when the call returns.
        webview.start(debug=self._cfg.debug, **icon_arg)
        log.info("[settings] window closed")
