"""macOS launchd LaunchAgent registration.

Writes ``~/Library/LaunchAgents/com.sayzo.agent.plist`` and runs
``launchctl load`` so the service auto-starts on login. No-op on non-darwin
platforms. Idempotent — safe to call on every successful first-run completion.

This logic used to live in ``installer/install.sh``. Moving it into the app
means the GUI installer (NSIS finish-page-launch / .app double-click) can
register the LaunchAgent itself, removing the requirement that a user paste
a terminal one-liner.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LAUNCH_AGENT_LABEL = "com.sayzo.agent"
LAUNCH_AGENT_PROGRAM = "/Applications/Sayzo Agent.app/Contents/MacOS/sayzo-agent"

# Verbatim from installer/macos/com.sayzo.agent.plist. Kept as a Python
# string so the running .app can write it without depending on the source
# file being shipped as a separate resource.
_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
        <string>service</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/tmp/sayzo-agent-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/sayzo-agent-stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def ensure_launchd_registered() -> bool:
    """Write the LaunchAgent plist and load it.

    Returns:
        True  if the plist was written/refreshed and ``launchctl load`` ran
        False if we skipped (non-darwin, or the .app isn't installed at the
              expected path so registration would be pointing at nothing)

    Never raises; logs and returns False on any error so the caller (the
    service start path) doesn't crash on a non-fatal registration failure.
    """
    if sys.platform != "darwin":
        return False

    program = Path(LAUNCH_AGENT_PROGRAM)
    if not program.exists():
        log.info(
            "skipping launchd registration: %s does not exist (dev run?)", program
        )
        return False

    plist_path = _plist_path()
    plist_body = _PLIST_TEMPLATE.format(
        label=LAUNCH_AGENT_LABEL, program=str(program)
    )

    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        # Compare to existing content before rewriting to avoid pointless
        # unload/load churn when nothing changed.
        existing = plist_path.read_text(encoding="utf-8") if plist_path.exists() else ""
        if existing != plist_body:
            plist_path.write_text(plist_body, encoding="utf-8")
            os.chmod(plist_path, 0o644)
            log.info("wrote launchd plist to %s", plist_path)
        else:
            log.info("launchd plist already up to date at %s", plist_path)

        # Unload first (no-op if not loaded) so a refreshed plist takes effect.
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
            check=False,
        )
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                "launchctl load returned %d: %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
    except OSError:
        log.warning("launchd registration failed", exc_info=True)
        return False

    log.info("launchd LaunchAgent %s registered", LAUNCH_AGENT_LABEL)
    return True
