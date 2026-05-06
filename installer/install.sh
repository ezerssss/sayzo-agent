#!/usr/bin/env bash
# Sayzo — macOS one-liner installer (dev / power-user path)
# Usage: curl -fsSL https://sayzo.app/releases/macos/install.sh | bash
#
# Downloads the DMG and copies the .app into /Applications. The launchd
# LaunchAgent registration that used to live here now happens inside the
# .app on first successful setup completion (see sayzo_agent/gui/setup/launchd.py)
# so the GUI installer path (drag DMG → drag .app → double-click) works
# without ever touching a terminal.

set -euo pipefail

ARCH=$(uname -m)  # x86_64 or arm64
DMG_NAME="Sayzo.dmg"
DOWNLOAD_URL="https://sayzo.app/releases/macos/${DMG_NAME}"
APP_NAME="Sayzo"
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
echo "  Sayzo Installer"
echo "  ==============="
echo ""

# -----------------------------------------------------------------------
# Download DMG
# -----------------------------------------------------------------------
echo "  Downloading Sayzo (${ARCH})..."
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

# Strip com.apple.quarantine recursively. Files copied from a mounted DMG
# inherit this xattr, and even though the DMG itself ships with a stapled
# Apple notarization ticket (Gatekeeper accepts it), removing the flag
# suppresses the standard Finder "downloaded from the internet, are you
# sure you want to open it?" dialog on first launch. No-op when already
# clean.
xattr -cr "$APP_PATH" 2>/dev/null || true

echo "  Installed to ${APP_PATH}"

# Launch via LaunchServices. The bundle is Developer-ID-signed and
# Apple-notarized in CI (see .github/workflows/build.yml — the
# codesign / notarytool / stapler steps), so spctl accepts it and `open`
# launches the .app cleanly. The earlier "spawn the inner Mach-O directly
# to bypass spctl" hack is gone with notarization.
echo ""
echo "  Opening Sayzo..."
open "$APP_PATH" --args service --force-setup || {
    echo "  Warning: 'open' failed to launch Sayzo." >&2
    echo "  Try opening Sayzo from Applications manually." >&2
}

echo ""
echo "  Done! Complete setup in the window that appears."
echo ""
