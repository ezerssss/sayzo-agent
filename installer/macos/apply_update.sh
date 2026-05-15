#!/bin/bash
# apply_update.sh — macOS swap-and-relaunch helper for Sayzo auto-update.
#
# Called detached by sayzo_agent.update_apply_mac.spawn_swap_helper_and_exit
# just before the agent exits. Waits for the agent's PID lock to release,
# mounts the staged DMG, replaces the live .app via rsync, unmounts, strips
# Gatekeeper quarantine, and relaunches via LaunchServices.
#
# Args:
#   $1  Absolute path to the staged Sayzo.dmg
#   $2  Absolute path to the live Sayzo.app (typically /Applications/Sayzo.app
#       but resolved by the Python caller from sys.executable, so it works
#       from ~/Applications and other non-standard install locations too)
#
# Exit code is informational — the agent that spawned us is already gone, so
# nobody reads it. All logs go to ~/.sayzo/agent/logs/apply_update.log so
# users can post-mortem a failed update.

set -u
set -o pipefail

DMG_PATH="${1:-}"
APP_PATH="${2:-}"

LOG_DIR="$HOME/.sayzo/agent/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/apply_update.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG_FILE" 2>/dev/null
}

fail() {
    log "FAIL: $*"
    exit 1
}

if [ -z "$DMG_PATH" ] || [ -z "$APP_PATH" ]; then
    fail "missing args (need DMG_PATH and APP_PATH)"
fi
if [ ! -f "$DMG_PATH" ]; then
    fail "DMG not found at $DMG_PATH"
fi
if [ ! -d "$APP_PATH" ]; then
    fail ".app bundle not found at $APP_PATH"
fi

log "starting apply: dmg=$DMG_PATH app=$APP_PATH"

# Wait for the agent process to fully release its PID flock. agent.pid uses
# fcntl.flock under the hood (sayzo_agent/pidfile.py:205-269) — the kernel
# releases the lock automatically the moment the agent process exits. We
# probe with `flock -n -x <fd>` to detect a held lock. Cap at 15s so a stuck
# agent can't block updates forever.
AGENT_PID_FILE="$HOME/.sayzo/agent/agent.pid"
WAITED=0
while [ "$WAITED" -lt 30 ]; do
    if [ ! -f "$AGENT_PID_FILE" ]; then
        break
    fi
    # macOS doesn't ship flock(1) by default. Probe by opening the file with
    # python and trying fcntl.flock LOCK_EX | LOCK_NB. /usr/bin/python3 ships
    # with Command Line Tools (which our users have via macOS itself for
    # codesigning the .app); fall back to a plain sleep if it's missing.
    if /usr/bin/python3 -c "
import fcntl, sys
try:
    f = open('$AGENT_PID_FILE', 'r+')
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    sys.exit(0)
except (OSError, BlockingIOError):
    sys.exit(1)
" 2>/dev/null; then
        log "agent.pid lock released after ${WAITED}*0.5s"
        break
    fi
    sleep 0.5
    WAITED=$((WAITED + 1))
done
if [ "$WAITED" -ge 30 ]; then
    log "WARN: agent.pid still locked after 15s, proceeding anyway"
fi

# Brief settle pause so any in-flight file handles fully tear down.
sleep 1

# Mount the staged DMG. -nobrowse keeps Finder from popping a window.
# Do NOT add -quiet here: it suppresses the device/mount-point table on
# stdout, leaving the parse below with nothing to chew on (root cause of
# the v3.1.x apply-fail loop on macOS — agent kept re-spawning the helper
# every boot because the helper exited 1 before reaching the DMG cleanup).
MOUNT_OUTPUT=$(hdiutil attach "$DMG_PATH" -nobrowse 2>&1) || \
    fail "hdiutil attach failed: $MOUNT_OUTPUT"
MOUNT_POINT=$(echo "$MOUNT_OUTPUT" | tail -n1 | awk '{for (i=3; i<=NF; i++) printf "%s ", $i; print ""}' | sed 's/[[:space:]]*$//')
if [ -z "$MOUNT_POINT" ] || [ ! -d "$MOUNT_POINT" ]; then
    fail "couldn't determine mount point from hdiutil output: $MOUNT_OUTPUT"
fi
log "mounted DMG at $MOUNT_POINT"

# Find the Sayzo.app inside the mounted DMG. Case-insensitive in case the
# bundle is renamed in a future release; first .app wins.
NEW_APP=""
for candidate in "$MOUNT_POINT"/*.app; do
    if [ -d "$candidate" ]; then
        NEW_APP="$candidate"
        break
    fi
done
if [ -z "$NEW_APP" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
    fail "no .app found in mounted DMG at $MOUNT_POINT"
fi
log "new app inside DMG: $NEW_APP"

# Atomic-feeling replace via rsync. -a preserves perms/symlinks/timestamps;
# --delete removes files that disappeared in the new release so we don't
# accumulate stale resources across upgrades. Trailing slashes matter:
# they mean "contents of src into dst" not "src into dst" (which would
# nest Sayzo.app/Sayzo.app).
if ! rsync -a --delete "$NEW_APP/" "$APP_PATH/" 2>>"$LOG_FILE"; then
    log "WARN: rsync exited non-zero, attempting unmount + relaunch anyway"
fi
log "rsync complete"

hdiutil detach "$MOUNT_POINT" -quiet 2>>"$LOG_FILE" || \
    log "WARN: hdiutil detach failed (mount left behind — harmless)"

# Strip Gatekeeper quarantine attribute on the freshly-copied bundle. The
# notarization ticket is stapled at .dmg-level; the .app inside inherits
# com.apple.quarantine from hdiutil, which would trigger the "downloaded
# from the internet" dialog on first launch otherwise.
xattr -cr "$APP_PATH" 2>>"$LOG_FILE" || \
    log "WARN: xattr -cr failed on $APP_PATH"

# Relaunch via LaunchServices so the new agent lives in the user's session.
# --args passes 'service' to the agent so it boots into the headless tray
# entrypoint, same as launchd's RunAtLoad path would.
if ! /usr/bin/open "$APP_PATH" --args service 2>>"$LOG_FILE"; then
    log "WARN: open failed for $APP_PATH"
fi
log "relaunch issued"

# Cleanup: remove the staged DMG. Leaving it would waste a few MB until the
# next download_and_stage overwrites it, but tidying up is friendlier.
rm -f "$DMG_PATH" 2>>"$LOG_FILE" || true

log "apply complete"
exit 0
