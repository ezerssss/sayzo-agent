"""Open a Sayzo GUI window standalone for visual review.

Usage:
    python scripts/preview_gui.py settings
    python scripts/preview_gui.py installer

Modes:
  * ``settings``    — in-app Settings window (pywebview + React).
  * ``installer``   — the pywebview/React installer flow that runs on
                      fresh install. This is the UI you'd see right after
                      running the NSIS installer for the first time.

Settings reads user_settings.json directly — no live agent needed.
Hotkey rebinds and JSON writes all hit the live ~/.sayzo/agent
directory, so changes in the preview really update stored settings —
revert with ``SAYZO_ARM__HOTKEY`` if needed.

Both modes use a one-shot ``webview.start()`` call. Run each in a
dedicated Python process — after it returns you can't open another
pywebview window from the same process.

The installer screens depend on what detect_setup finds:
  * no token          → Welcome (sign-in)
  * token, no model   → Download
  * Windows, complete → NotificationsWin
  * macOS, complete   → Permissions or Done
To force the permissions screen on macOS, delete the marker file:
  rm ~/.sayzo/agent/.permissions_onboarded_v1
To force the sign-in screen, delete ~/.sayzo/agent/auth.json.
"""
from __future__ import annotations

import sys


def main() -> int:
    valid = {"settings", "installer"}
    if len(sys.argv) < 2 or sys.argv[1] not in valid:
        print(__doc__)
        return 1

    target = sys.argv[1]
    from sayzo_agent.config import load_config

    cfg = load_config()

    if target == "installer":
        return _run_installer(cfg)

    from sayzo_agent.gui.settings.window import SettingsWindow

    SettingsWindow(cfg).run_blocking()
    return 0


def _run_installer(cfg) -> int:
    from sayzo_agent.gui.setup.window import SetupWindow

    result = SetupWindow(cfg).run_blocking()
    print(f"installer closed: {result.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
