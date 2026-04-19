#!/usr/bin/env bash
# Sayzo Agent — macOS one-liner installer (dev / power-user path)
# Usage: curl -fsSL https://sayzo.app/releases/macos/install.sh | bash
#
# Downloads the DMG and copies the .app into /Applications. The launchd
# LaunchAgent registration that used to live here now happens inside the
# .app on first successful setup completion (see sayzo_agent/gui/setup/launchd.py)
# so the GUI installer path (drag DMG → drag .app → double-click) works
# without ever touching a terminal.

set -euo pipefail

VERSION="0.1.0"
ARCH=$(uname -m)  # x86_64 or arm64
DMG_NAME="Sayzo-Agent-${VERSION}.dmg"
DOWNLOAD_URL="https://sayzo.app/releases/macos/${DMG_NAME}"
APP_NAME="Sayzo Agent"
APP_PATH="/Applications/${APP_NAME}.app"
TMPDIR_INSTALL=$(mktemp -d)

cleanup() {
    if [ -d "/Volumes/${APP_NAME}" ]; then
        hdiutil detach "/Volumes/${APP_NAME}" -quiet 2>/dev/null || true
    fi
    rm -rf "$TMPDIR_INSTALL"
}
trap cleanup EXIT

echo ""
echo "  Sayzo Agent Installer"
echo "  ====================="
echo ""

# -----------------------------------------------------------------------
# Download DMG
# -----------------------------------------------------------------------
echo "  Downloading Sayzo Agent v${VERSION} (${ARCH})..."
DMG_PATH="${TMPDIR_INSTALL}/${DMG_NAME}"
if ! curl -fSL -o "$DMG_PATH" "$DOWNLOAD_URL"; then
    echo "  Download failed." >&2
    echo "  URL: $DOWNLOAD_URL" >&2
    exit 1
fi
echo "  Downloaded."

# -----------------------------------------------------------------------
# Mount DMG and copy .app to /Applications
# -----------------------------------------------------------------------
echo "  Installing to /Applications..."
hdiutil attach "$DMG_PATH" -nobrowse -quiet
if [ -d "/Volumes/${APP_NAME}/${APP_NAME}.app" ]; then
    [ -d "$APP_PATH" ] && rm -rf "$APP_PATH"
    cp -R "/Volumes/${APP_NAME}/${APP_NAME}.app" "/Applications/"
else
    echo "  Error: .app not found in DMG." >&2
    exit 1
fi
hdiutil detach "/Volumes/${APP_NAME}" -quiet
echo "  Installed to ${APP_PATH}"

# Launch the inner binary directly (NOT `open` on the .app bundle) so we
# bypass Gatekeeper's notarization check. Without an Apple notarization
# stamp, `spctl` rejects the .app and `open` silently refuses to launch —
# no dialog, no error, the app just doesn't start. Invoking the Mach-O
# binary directly from the shell skips LaunchServices and therefore skips
# Gatekeeper. The launchd LaunchAgent we register during first-run uses the
# same path, so subsequent auto-starts on login also bypass Gatekeeper.
#
# Trade-off: double-clicking the .app from Finder will still be blocked
# until the release is properly notarized. Users should interact via the
# tray/menu bar icon for day-to-day control, not by re-opening the .app.
#
# Detach so the shell can return — `nohup … &` + `disown` keeps the agent
# running after this script exits.
SAYZO_BIN="${APP_PATH}/Contents/MacOS/sayzo-agent"
if [ -x "$SAYZO_BIN" ]; then
    echo ""
    echo "  Opening Sayzo Agent..."
    nohup "$SAYZO_BIN" service --force-setup >/tmp/sayzo-agent-bootstrap.log 2>&1 &
    disown
else
    echo "  Warning: could not find $SAYZO_BIN" >&2
    echo "  Try opening Sayzo Agent from Applications manually." >&2
fi

echo ""
echo "  Done! Complete setup in the window that appears."
echo ""
