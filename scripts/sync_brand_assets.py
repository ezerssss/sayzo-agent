"""Copy brand assets from installer/assets/ into the webui src/assets/.

Vite's bundler can't import from outside the webui project root without
fighting the file-system protection. Rather than ship a fragile alias,
we keep ``installer/assets/`` as the single source of truth and mirror
the brand files into ``sayzo_agent/gui/webui/src/assets/`` before the
React build runs.

Wired as the ``prebuild`` npm script — runs automatically before
``npm run build`` and ``npm run dev``.

Usage:
    python scripts/sync_brand_assets.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "installer" / "assets"
TARGET = REPO_ROOT / "sayzo_agent" / "gui" / "webui" / "src" / "assets"

# Files that the React bundle imports. Add new entries here as the UI
# grows — anything not listed is left alone in the target dir.
SYNCED = ["logo.png"]


def main() -> int:
    if not SOURCE.is_dir():
        print(f"[sync-brand] source missing: {SOURCE}", file=sys.stderr)
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)
    for name in SYNCED:
        src = SOURCE / name
        dst = TARGET / name
        if not src.exists():
            print(f"[sync-brand] skipped (no source): {src}", file=sys.stderr)
            continue
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size == src.stat().st_size:
            continue
        shutil.copy2(src, dst)
        print(f"[sync-brand] {src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
