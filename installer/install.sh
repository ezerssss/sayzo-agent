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

# Launch the .app now so the setup window opens automatically — matches the
# GUI-install UX where double-clicking the app in Finder triggers first-run.
# Uses the explicit path rather than `open -a "Name"` because LaunchServices
# hasn't finished indexing the freshly-copied bundle yet, so name lookup
# fails with "unable to find application". An explicit path sidesteps that.
echo ""
echo "  Opening Sayzo Agent..."
open "${APP_PATH}"

echo ""
echo "  Done! Complete setup in the window that appears."
echo ""
