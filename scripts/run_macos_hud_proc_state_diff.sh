#!/bin/bash
# Runs scripts/probe_macos_hud_proc_state.py in 3 scenarios + diffs them.
#
# Scenario A — terminal-spawn HUD solo (KNOWN VISIBLE baseline)
# Scenario B — production HUD with Finder-launched agent (KNOWN INVISIBLE bug case)
# Scenario C — terminal-spawn HUD WITH Finder-launched agent also running (KNOWN VISIBLE — the contradiction)
#
# Diffing B vs C tells us the literal trigger of the bug: both scenarios
# have a Finder-launched agent running with full LaunchServices env vars;
# the only difference is HOW the HUD subprocess is spawned. Whatever
# differs in B vs C is the root cause.
#
# Usage: bash scripts/run_macos_hud_proc_state_diff.sh
# Requires: python3 with pyobjc-framework-Cocoa installed.
# May prompt for sudo (for lsmp Mach port listing).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROBE="$SCRIPT_DIR/probe_macos_hud_proc_state.py"
OUT_DIR="$HOME/Downloads"
BINARY="/Applications/Sayzo.app/Contents/MacOS/sayzo-agent"

if [ ! -f "$BINARY" ]; then
  echo "ERROR: $BINARY not found. Is Sayzo installed?"
  exit 1
fi
if [ ! -f "$PROBE" ]; then
  echo "ERROR: probe script not found at $PROBE"
  exit 1
fi

echo "Output directory: $OUT_DIR"
mkdir -p "$OUT_DIR"

# Heads-up about sudo prompt
echo
echo "=========================================================================="
echo "  This script captures process state in 3 scenarios. lsmp (Mach port"
echo "  listing) usually needs sudo for full output — you'll be prompted once."
echo "=========================================================================="
echo

# Trigger sudo prompt now so it doesn't interrupt mid-flow
sudo -v || { echo "sudo required for lsmp"; exit 1; }


run_probe() {
  local pid="$1"
  local outfile="$2"
  echo "  Capturing state for PID $pid → $outfile"
  sudo python3 "$PROBE" --pid "$pid" > "$outfile" 2>&1
}


cleanup() {
  killall -9 sayzo-agent 2>/dev/null || true
  killall -9 Sayzo 2>/dev/null || true
  sleep 1
}


# ---------------------------------------------------------------------------
# Scenario A — terminal-spawn HUD solo (no parent agent)
# ---------------------------------------------------------------------------
echo
echo "=== SCENARIO A: terminal-spawn HUD solo (KNOWN VISIBLE) ==="
cleanup
"$BINARY" hud --demo &
HUD_PID=$!
echo "  Spawned HUD PID: $HUD_PID"
sleep 4
run_probe "$HUD_PID" "$OUT_DIR/proc_A_terminal_solo.txt"
kill "$HUD_PID" 2>/dev/null || true
sleep 2


# ---------------------------------------------------------------------------
# Scenario B — production HUD spawned by Finder-launched agent
# ---------------------------------------------------------------------------
echo
echo "=== SCENARIO B: production HUD with Finder-launched agent (KNOWN INVISIBLE) ==="
cleanup
open /Applications/Sayzo.app
echo "  Waiting 6s for agent to spawn HUD subprocess..."
sleep 6
HUD_PID=$(ps -axww -o pid,command | awk '/\/Applications\/Sayzo\.app\/Contents\/MacOS\/sayzo-agent hud/ {print $1; exit}')
PARENT_PID=$(ps -axww -o pid,command | awk '/\/Applications\/Sayzo\.app\/Contents\/MacOS\/sayzo-agent/ && !/--idle/ {print $1; exit}')
if [ -z "$HUD_PID" ]; then
  echo "  ERROR: could not find HUD subprocess. Sayzo may not be running."
  echo "  Skipping Scenario B."
else
  echo "  Parent agent PID: $PARENT_PID  HUD subprocess PID: $HUD_PID"
  run_probe "$HUD_PID" "$OUT_DIR/proc_B_finder_production.txt"
  # Also capture the parent for reference
  if [ -n "$PARENT_PID" ]; then
    run_probe "$PARENT_PID" "$OUT_DIR/proc_B_finder_parent_for_reference.txt"
  fi
fi
sleep 1


# ---------------------------------------------------------------------------
# Scenario C — terminal-spawn HUD WHILE Finder-launched agent runs
# ---------------------------------------------------------------------------
echo
echo "=== SCENARIO C: terminal-spawn HUD WITH Finder-launched agent (KNOWN VISIBLE — contradiction) ==="
# Don't cleanup — leave the Finder-launched agent running from B
# Kill only the production HUD subprocess so we have a clean parent
if [ -n "$HUD_PID" ]; then
  kill -9 "$HUD_PID" 2>/dev/null || true
  sleep 2
fi
"$BINARY" hud --demo &
HUD_PID_C=$!
echo "  Spawned terminal-HUD PID: $HUD_PID_C (alongside Finder-launched agent PID $PARENT_PID)"
sleep 4
run_probe "$HUD_PID_C" "$OUT_DIR/proc_C_terminal_with_finder_parent.txt"
kill "$HUD_PID_C" 2>/dev/null || true
cleanup


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
echo
echo "=========================================================================="
echo "  CAPTURE COMPLETE"
echo "=========================================================================="
echo
echo "Files saved to $OUT_DIR/:"
ls -la "$OUT_DIR"/proc_*.txt 2>/dev/null

echo
echo "=== diff B (broken) vs C (working) — THE DECISIVE COMPARISON ==="
diff "$OUT_DIR/proc_B_finder_production.txt" "$OUT_DIR/proc_C_terminal_with_finder_parent.txt" \
  > "$OUT_DIR/diff_B_vs_C.txt" 2>&1
diff_lines=$(wc -l < "$OUT_DIR/diff_B_vs_C.txt")
echo "Diff B vs C is $diff_lines lines long. Saved to $OUT_DIR/diff_B_vs_C.txt"
echo

echo "=== diff A (terminal-solo, working) vs B (production, broken) ==="
diff "$OUT_DIR/proc_A_terminal_solo.txt" "$OUT_DIR/proc_B_finder_production.txt" \
  > "$OUT_DIR/diff_A_vs_B.txt" 2>&1
echo "Diff A vs B saved to $OUT_DIR/diff_A_vs_B.txt"

echo
echo "=========================================================================="
echo "  Send back the contents of:"
echo "    $OUT_DIR/diff_B_vs_C.txt    (most important — the root-cause diff)"
echo "    $OUT_DIR/diff_A_vs_B.txt    (sanity check)"
echo "    $OUT_DIR/proc_B_finder_production.txt  (full broken-state dump)"
echo "=========================================================================="
