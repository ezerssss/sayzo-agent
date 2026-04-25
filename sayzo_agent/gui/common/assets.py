"""Asset path resolution shared by every pywebview window.

Both the setup wizard and the Settings window load the same
``gui/webui/dist/index.html`` bundle and prefer the same Sayzo logo for the
taskbar/dock icon. The dev/frozen branching is identical — extracting it
keeps the two window orchestrators down to dimensions + URL construction
and means a future installer-layout change updates one file.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def webui_index_path() -> Path:
    """Resolve the path to ``index.html`` in dev and frozen builds.

    Frozen: ``<sys._MEIPASS>/sayzo_agent/gui/webui/dist/index.html``
    Dev:    ``<repo>/sayzo_agent/gui/webui/dist/index.html``
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "sayzo_agent" / "gui" / "webui" / "dist"  # type: ignore[attr-defined]
    else:
        # __file__ is .../sayzo_agent/gui/common/assets.py — climb to gui/.
        base = Path(__file__).resolve().parent.parent / "webui" / "dist"
    return base / "index.html"


def icon_path() -> Optional[Path]:
    """Pick the Sayzo logo to pass to pywebview.

    On Windows ``.ico`` renders sharper in the taskbar; elsewhere we use
    PNG. Returns None if no asset is bundled, so callers can fall back to
    pywebview's default icon without a special-case branch.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    else:
        # gui/common/assets.py — climb to repo root.
        base = Path(__file__).resolve().parent.parent.parent.parent / "installer" / "assets"
    if sys.platform == "win32":
        ico = base / "logo.ico"
        if ico.exists():
            return ico
    png = base / "logo.png"
    if png.exists():
        return png
    return None
