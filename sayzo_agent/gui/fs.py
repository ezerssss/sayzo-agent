"""Small cross-platform GUI filesystem helpers."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def open_folder(path: Path) -> None:
    """Open ``path`` in the platform's file manager (Explorer / Finder / xdg-open)."""
    try:
        p = str(path)
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        log.warning("[fs] open_folder failed for %s", path, exc_info=True)
