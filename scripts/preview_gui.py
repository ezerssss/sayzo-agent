"""Open a Sayzo GUI window standalone for visual review.

Usage:
    python scripts/preview_gui.py settings
    python scripts/preview_gui.py onboarding
    python scripts/preview_gui.py installer

Modes:
  * ``settings``    — in-app Settings window (tkinter + Sayzo theme).
  * ``onboarding``  — first-run walkthrough (tkinter + Sayzo theme).
  * ``installer``   — the pywebview/React installer flow that runs on
                      fresh install. This is the UI you'd see right after
                      running the NSIS installer for the first time.

No agent service needs to be running — we construct a stub Agent just
far enough to hand the windows an ArmController. Arm/disarm, hotkey
rebind, and user_settings.json writes all work against the live
~/.sayzo/agent directory, so picking a new hotkey in Settings or
Onboarding really does update your stored settings — revert with
``SAYZO_ARM__HOTKEY`` if needed.

Notes on ``installer`` mode:

- The installer uses a one-shot ``webview.start()`` call. Run this in a
  dedicated Python process — after it returns you can't open another
  pywebview window from the same process.
- The screens you see depend on what detect_setup finds:
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
    valid = {"settings", "onboarding", "installer"}
    if len(sys.argv) < 2 or sys.argv[1] not in valid:
        print(__doc__)
        return 1

    target = sys.argv[1]
    from sayzo_agent.config import load_config

    cfg = load_config()

    if target == "installer":
        return _run_installer(cfg)

    # settings / onboarding both need a live ArmController.
    from sayzo_agent.app import Agent
    agent = Agent(cfg)

    if target == "settings":
        from sayzo_agent.gui.settings_window import open_settings_window
        open_settings_window(cfg, agent.arm)
    else:
        from sayzo_agent.onboarding import open_onboarding_window
        open_onboarding_window(cfg, agent.arm)
    return 0


def _run_installer(cfg) -> int:
    from sayzo_agent.gui.setup.window import SetupWindow

    result = SetupWindow(cfg).run_blocking()
    print(f"installer closed: {result.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
