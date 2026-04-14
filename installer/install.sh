#!/usr/bin/env bash
# Eloquy Agent — macOS one-liner installer
# Usage: curl -fsSL https://eloquy.threadlify.io/releases/macos/install.sh | bash
#
# Downloads the DMG, installs the .app, sets up launchd auto-start,
# and launches first-run setup.

set -euo pipefail

VERSION="0.1.0"
ARCH=$(uname -m)  # x86_64 or arm64
DMG_NAME="Eloquy-Agent-${VERSION}.dmg"
DOWNLOAD_URL="https://eloquy.threadlify.io/releases/macos/${DMG_NAME}"
APP_NAME="Eloquy Agent"
APP_PATH="/Applications/${APP_NAME}.app"
PLIST_NAME="com.eloquy.agent.plist"
PLIST_SRC_URL="https://raw.githubusercontent.com/eloquy/agent/main/installer/macos/${PLIST_NAME}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
TMPDIR_INSTALL=$(mktemp -d)

cleanup() {
    # Unmount DMG if still mounted.
    if [ -d "/Volumes/${APP_NAME}" ]; then
        hdiutil detach "/Volumes/${APP_NAME}" -quiet 2>/dev/null || true
    fi
    rm -rf "$TMPDIR_INSTALL"
}
trap cleanup EXIT

echo ""
echo "  Eloquy Agent Installer"
echo "  ======================"
echo ""

# -----------------------------------------------------------------------
# Download DMG
# -----------------------------------------------------------------------
echo "  Downloading Eloquy Agent v${VERSION} (${ARCH})..."
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
    # Remove old install if present.
    [ -d "$APP_PATH" ] && rm -rf "$APP_PATH"
    cp -R "/Volumes/${APP_NAME}/${APP_NAME}.app" "/Applications/"
else
    echo "  Error: .app not found in DMG." >&2
    exit 1
fi
hdiutil detach "/Volumes/${APP_NAME}" -quiet
echo "  Installed to ${APP_PATH}"

# -----------------------------------------------------------------------
# Install launchd plist for auto-start
# -----------------------------------------------------------------------
echo "  Setting up auto-start..."
mkdir -p "$LAUNCH_AGENTS_DIR"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

# Unload existing plist if present (ignore errors).
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# Write the plist.  We embed it directly instead of downloading to avoid
# a network dependency on the raw GitHub URL.
cat > "$PLIST_PATH" << 'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.eloquy.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/Eloquy Agent.app/Contents/MacOS/eloquy-agent</string>
        <string>service</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/eloquy-agent-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/eloquy-agent-stderr.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

echo "  Launchd plist installed at ${PLIST_PATH}"

# -----------------------------------------------------------------------
# Launch first-run setup
# -----------------------------------------------------------------------
echo ""
echo "  Launching first-time setup..."
"${APP_PATH}/Contents/MacOS/eloquy-agent" first-run

# -----------------------------------------------------------------------
# Load the launchd service
# -----------------------------------------------------------------------
launchctl load "$PLIST_PATH"

echo ""
echo "  Done! Eloquy Agent will start automatically on login."
echo ""
