#!/bin/bash
# Local end-to-end validation of the Helper.app pattern (v3.2.0) BEFORE
# wiring it into PyInstaller spec / CI / launcher.py.
#
# What this does:
#   1. Builds installer/macos/sayzo_hud_wrapper.c → SayzoHud
#   2. Assembles installer/macos/SayzoHud.app/ as a complete bundle
#   3. Drops the assembled SayzoHud.app into
#      /Applications/Sayzo.app/Contents/Frameworks/SayzoHud.app/
#   4. Ad-hoc resigns the nested bundle so it's loadable
#   5. Launches Sayzo agent via Finder (LaunchServices path)
#   6. Spawns a HUD subprocess via the wrapper from a terminal
#      (parent = shell — NOT exactly production-equivalent, but close
#      enough to verify the wrapper itself works + that the spawned HUD
#      gets bundleID populated + CGS connection)
#   7. Dumps lsappinfo for the spawned HUD
#   8. Reports verdict
#
# What this canNOT validate (advisor caveat):
#   The wrapper running with the actual sayzo-agent agent process as its
#   parent. That requires modifying launcher.py + rebuilding the bundle
#   (PyInstaller .pyc files inside the bundle aren't easy to patch in place).
#   The closest we can do is: confirm the wrapper-spawned HUD has bundleID
#   AND CGS connection populated when the wrapper itself is a child of
#   the shell — strong evidence (though not proof) that it'll work when
#   the wrapper is a child of the agent in production.
#
# Pass:    Spawned HUD has bundleID="com.sayzo.agent" and CGS connection
# Fail:    Spawned HUD has bundleID=[NULL] / !cgsConnection (same as
#          production bug — Helper.app doesn't fix it, fall back to
#          Architecture B or different approach)
#
# Usage:   bash scripts/validate_helper_app.sh
# Requires: sudo (for codesign + writing into /Applications/), Xcode CLT (for cc)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_SCRIPT="$REPO_DIR/installer/macos/build_sayzo_hud_wrapper.sh"
HELPER_BUNDLE_SRC="$REPO_DIR/installer/macos/SayzoHud.app"
SAYZO_BUNDLE="/Applications/Sayzo.app"
HELPER_BUNDLE_DEST="$SAYZO_BUNDLE/Contents/Frameworks/SayzoHud.app"
HUD_BIN="$SAYZO_BUNDLE/Contents/MacOS/sayzo-agent"
WRAPPER_BIN="$HELPER_BUNDLE_DEST/Contents/MacOS/SayzoHud"

cleanup() {
  killall -9 sayzo-agent 2>/dev/null || true
  killall -9 Sayzo 2>/dev/null || true
  killall -9 SayzoHud 2>/dev/null || true
  sleep 1
}

if [ ! -d "$SAYZO_BUNDLE" ]; then
  echo "ERROR: $SAYZO_BUNDLE not found"
  exit 1
fi
if [ ! -f "$BUILD_SCRIPT" ]; then
  echo "ERROR: build script not found at $BUILD_SCRIPT"
  exit 1
fi


echo "============================================================"
echo " Step 1: Build sayzo_hud_wrapper into the helper bundle skeleton"
echo "============================================================"
bash "$BUILD_SCRIPT"
ls -la "$HELPER_BUNDLE_SRC/Contents/MacOS/"


echo
echo "============================================================"
echo " Step 2: Verify helper bundle skeleton structure"
echo "============================================================"
ls -laR "$HELPER_BUNDLE_SRC/" | head -25
if [ ! -f "$HELPER_BUNDLE_SRC/Contents/Info.plist" ]; then
  echo "ERROR: Info.plist missing"
  exit 1
fi
if [ ! -x "$HELPER_BUNDLE_SRC/Contents/MacOS/SayzoHud" ]; then
  echo "ERROR: SayzoHud binary missing or not executable"
  exit 1
fi


echo
echo "============================================================"
echo " Step 3: Install assembled helper bundle into /Applications/Sayzo.app"
echo "============================================================"
echo "Will: sudo cp -R $HELPER_BUNDLE_SRC $HELPER_BUNDLE_DEST"
sudo rm -rf "$HELPER_BUNDLE_DEST"
sudo cp -R "$HELPER_BUNDLE_SRC" "$HELPER_BUNDLE_DEST"
sudo chmod +x "$WRAPPER_BIN"
ls -la "$HELPER_BUNDLE_DEST/Contents/MacOS/"


echo
echo "============================================================"
echo " Step 4: Ad-hoc resign the nested helper bundle"
echo "============================================================"
sudo codesign --force --sign - "$HELPER_BUNDLE_DEST"
codesign -dvv "$HELPER_BUNDLE_DEST" 2>&1 | head -10


echo
echo "============================================================"
echo " Step 5: Launch agent normally (Finder/LaunchServices)"
echo "============================================================"
cleanup
open "$SAYZO_BUNDLE"
echo "Waiting 5s for agent to come up..."
sleep 5

AGENT_PID=$(ps -axww -o pid,command | awk \
  "/\\/Applications\\/Sayzo\\.app\\/Contents\\/MacOS\\/sayzo-agent/ && !/--idle/ {print \$1; exit}")
if [ -z "$AGENT_PID" ]; then
  echo "ERROR: agent didn't launch."
  exit 1
fi
echo "Agent PID: $AGENT_PID"


echo
echo "############################################################"
echo "# RUNNING WRAPPER TEST (the v3.2.0 fix)"
echo "############################################################"
echo "Command: $WRAPPER_BIN $HUD_BIN hud --demo"
echo
echo "Watch top-right of screen. The HUD demo should render."
echo
"$WRAPPER_BIN" "$HUD_BIN" hud --demo &
WRAPPER_PID=$!
echo "Wrapper PID: $WRAPPER_PID"
sleep 4

# Find the actual HUD subprocess (child of wrapper running sayzo-agent hud --demo)
TEST_HUD_PID=$(ps -axww -o pid,ppid,command | awk \
  -v wp="$WRAPPER_PID" '$2 == wp && /sayzo-agent hud --demo/ {print $1; exit}')
if [ -z "$TEST_HUD_PID" ]; then
  echo "WARN: couldn't find wrapper child by ppid match. Trying loose match..."
  TEST_HUD_PID=$(ps -axww -o pid,command | awk '/sayzo-agent hud --demo/ {print $1; exit}')
fi

if [ -z "$TEST_HUD_PID" ]; then
  echo "ERROR: HUD subprocess not found"
  kill -9 "$WRAPPER_PID" 2>/dev/null || true
  exit 1
fi

echo
echo "Test HUD PID: $TEST_HUD_PID"
echo
echo "--- Process tree ---"
ps -axww -o pid,ppid,command | grep -E "($AGENT_PID|$WRAPPER_PID|$TEST_HUD_PID)" | grep -v grep || true

echo
echo "--- lsappinfo info $TEST_HUD_PID ---"
INFO=$(lsappinfo info "$TEST_HUD_PID" 2>&1)
echo "$INFO"

echo
echo "--- launchctl procinfo $TEST_HUD_PID (responsible path + audit) ---"
sudo launchctl procinfo "$TEST_HUD_PID" 2>&1 | grep -E "(responsible|argument vector|bsd proc)" -A2 | head -20 || true

echo
echo "============================================================"
echo " VERDICT"
echo "============================================================"
if echo "$INFO" | grep -q "!cgsConnection"; then
  echo "✗ FAIL: HUD has NO CGS CONNECTION (still classified as helper)"
  echo "  Helper.app pattern alone isn't sufficient. Architecture A failed."
  echo "  Possible next step: Architecture B (full separate PyInstaller bundle)"
elif echo "$INFO" | grep -qE 'bundleID="(com\.sayzo\.agent|com\.sayzo\.agent\.hud)"'; then
  echo "✓ PASS: HUD has CGS connection AND is properly registered with LaunchServices"
  echo "  bundleID found in lsappinfo output above"
  echo
  echo "  → Wire up launcher.py to spawn the helper, add CI build step,"
  echo "    bump pyproject.toml to 3.2.0, push v3.2.0."
else
  echo "? PARTIAL: lsappinfo state is unusual. Inspect output above carefully."
fi


echo
echo "============================================================"
echo " Cleanup"
echo "============================================================"
sleep 4
kill -9 "$TEST_HUD_PID" 2>/dev/null || true
kill -9 "$WRAPPER_PID" 2>/dev/null || true
cleanup
echo
echo "Note: The helper bundle remains installed at:"
echo "  $HELPER_BUNDLE_DEST"
echo "And the parent Sayzo.app is now ad-hoc resigned (no longer Developer-ID-notarized)."
echo "If you want to revert, reinstall Sayzo.app from the v3.1.7 DMG."
