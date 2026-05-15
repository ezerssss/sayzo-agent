#!/bin/bash
# Local end-to-end validation of the disclaim spawner fix BEFORE pushing v3.1.7.
#
# What this does:
#   1. Compiles installer/macos/hud_disclaim_spawner.c
#   2. Copies the binary into /Applications/Sayzo.app/Contents/MacOS/ (so
#      LaunchServices attributes it to the Sayzo bundle, matching production)
#   3. Re-signs the bundle ad-hoc so the new binary is loadable
#   4. Launches the agent via Finder (LaunchServices path, like production)
#   5. Uses launchctl bsexec + the disclaim spawner to spawn a test HUD
#      INSIDE the agent's bootstrap context with disclaim ON
#   6. Dumps the resulting HUD's lsappinfo state — checks for CGS connection
#   7. Then runs the same test WITHOUT disclaim (control)
#   8. Reports verdict
#
# Pass:    HUD has CGS connection with disclaim, NOT without → ship the fix
# Fail:    HUD lacks CGS connection in both → disclaim alone isn't enough,
#          fall back to Helper.app pattern
#
# Usage:   bash scripts/validate_disclaim_fix.sh
# Requires: sudo (for codesign + bsexec), Xcode Command Line Tools (for cc)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_SCRIPT="$REPO_DIR/installer/macos/build_hud_disclaim_spawner.sh"
BUNDLE="/Applications/Sayzo.app"
SPAWNER_DEST="$BUNDLE/Contents/MacOS/hud_disclaim_spawner"
HUD_BIN="$BUNDLE/Contents/MacOS/sayzo-agent"
OUT_DIR="$HOME/Downloads"

cleanup() {
  killall -9 sayzo-agent 2>/dev/null || true
  killall -9 Sayzo 2>/dev/null || true
  sleep 1
}

if [ ! -d "$BUNDLE" ]; then
  echo "ERROR: $BUNDLE not found"
  exit 1
fi
if [ ! -f "$BUILD_SCRIPT" ]; then
  echo "ERROR: $BUILD_SCRIPT not found"
  exit 1
fi

echo "============================================================"
echo " Step 1: Build hud_disclaim_spawner"
echo "============================================================"
bash "$BUILD_SCRIPT" "$REPO_DIR/dist"
LOCAL_SPAWNER="$REPO_DIR/dist/hud_disclaim_spawner"
ls -l "$LOCAL_SPAWNER"

echo
echo "============================================================"
echo " Step 2: Install spawner into bundle (sudo for /Applications/)"
echo "============================================================"
echo "Will: sudo cp $LOCAL_SPAWNER $SPAWNER_DEST"
sudo cp "$LOCAL_SPAWNER" "$SPAWNER_DEST"
sudo chmod +x "$SPAWNER_DEST"

echo
echo "============================================================"
echo " Step 3: Ad-hoc sign the new binary so it's loadable"
echo "============================================================"
sudo codesign --force --sign - "$SPAWNER_DEST"
codesign -dvv "$SPAWNER_DEST" 2>&1 | head -15

echo
echo "============================================================"
echo " Step 4: Launch the agent normally (Finder/LaunchServices)"
echo "============================================================"
cleanup
open /Applications/Sayzo.app
echo "Waiting 5s for agent to come up..."
sleep 5

AGENT_PID=$(ps -axww -o pid,command | awk \
  '/\/Applications\/Sayzo\.app\/Contents\/MacOS\/sayzo-agent/ && !/--idle/ {print $1; exit}')
if [ -z "$AGENT_PID" ]; then
  echo "ERROR: agent didn't launch. Check Sayzo manually."
  exit 1
fi
echo "Agent PID: $AGENT_PID"

# Kill the agent's normal HUD subprocess so it doesn't conflict with
# our test HUD. The agent will respawn it but it'll race with our test;
# we don't care about it since we're using bsexec to spawn our own.
EXISTING_HUD=$(ps -axww -o pid,command | awk \
  '/\/Applications\/Sayzo\.app\/Contents\/MacOS\/sayzo-agent hud/ {print $1; exit}')
if [ -n "$EXISTING_HUD" ]; then
  echo "Killing agent's existing HUD subprocess (PID $EXISTING_HUD) so it doesn't race"
  kill -9 "$EXISTING_HUD" 2>/dev/null || true
  sleep 1
fi


run_one_test() {
  local label="$1"
  local cmd="$2"
  echo
  echo "============================================================"
  echo " TEST: $label"
  echo "============================================================"
  echo "Command: $cmd"
  echo
  echo "Watch top-right of screen. The HUD demo SHOULD render a small"
  echo "magenta-ish UI shortly. We'll dump lsappinfo + kill in 8s."
  echo
  # Run in background so we can dump lsappinfo while it's alive
  eval "$cmd" >/dev/null 2>&1 &
  local launchctl_pid=$!
  sleep 4

  # The HUD should have spawned by now. Find its PID — child of either
  # the launchctl invocation or its grandchild via the spawner.
  local TEST_HUD_PID
  TEST_HUD_PID=$(ps -axww -o pid,command | awk '/sayzo-agent hud --demo/ {print $1; exit}')
  if [ -z "$TEST_HUD_PID" ]; then
    echo "ERROR: test HUD process not found"
    kill -9 "$launchctl_pid" 2>/dev/null || true
    return 1
  fi
  echo "Test HUD PID: $TEST_HUD_PID"
  echo
  echo "--- lsappinfo info $TEST_HUD_PID ---"
  lsappinfo info "$TEST_HUD_PID" 2>&1 | head -30
  echo
  local INFO
  INFO=$(lsappinfo info "$TEST_HUD_PID" 2>&1)
  if echo "$INFO" | grep -q "!cgsConnection"; then
    echo "VERDICT [$label]: ✗ NO CGS CONNECTION (HUD will be invisible)"
  elif echo "$INFO" | grep -q '"com.sayzo.agent"'; then
    echo "VERDICT [$label]: ✓ HAS CGS CONNECTION + REGISTERED AS com.sayzo.agent"
  else
    echo "VERDICT [$label]: ? mixed/unknown — inspect lsappinfo above"
  fi

  sleep 4
  kill -9 "$TEST_HUD_PID" 2>/dev/null || true
  kill -9 "$launchctl_pid" 2>/dev/null || true
  sleep 1
}


echo
echo "############################################################"
echo "# RUNNING DISCLAIM-ON TEST (the fix)"
echo "############################################################"
run_one_test "disclaim ON" \
  "sudo launchctl bsexec '$AGENT_PID' '$SPAWNER_DEST' '$HUD_BIN' hud --demo"


echo
echo "############################################################"
echo "# RUNNING DISCLAIM-OFF TEST (control — current production)"
echo "############################################################"
# Without disclaim — directly launch HUD via bsexec, no spawner wrapper.
# This mimics what current launcher.py does (asyncio.create_subprocess_exec
# with no disclaim attr).
run_one_test "disclaim OFF (control)" \
  "sudo launchctl bsexec '$AGENT_PID' '$HUD_BIN' hud --demo"


echo
echo "============================================================"
echo " Cleanup"
echo "============================================================"
cleanup
echo "Agent stopped. The spawner binary remains installed at:"
echo "  $SPAWNER_DEST"
echo "If you want to remove it (the bundle is now ad-hoc signed and"
echo "may show as 'modified'):"
echo "  sudo rm $SPAWNER_DEST"
echo "  # And restore original codesign — best to reinstall Sayzo.app from DMG"

echo
echo "============================================================"
echo " EXPECTED RESULTS:"
echo "============================================================"
echo " ✓ disclaim ON  → HAS CGS connection (HUD visible)"
echo " ✗ disclaim OFF → NO CGS connection (HUD invisible)"
echo
echo " If both match expectation, the disclaim spawner is the fix."
echo " Code launcher.py to use the spawner, push v3.1.7."
echo
echo " If disclaim ON ALSO has no CGS connection, fall back to Helper.app."
