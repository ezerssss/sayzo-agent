/*
 * sayzo_hud_wrapper.c — tiny native binary that is the entry point of
 * SayzoHud.app (the nested Helper.app inside Sayzo.app/Contents/Frameworks/).
 *
 * Why this exists
 * ===============
 *
 * The HUD subprocess (PySide6 + QtWebEngine) renders invisibly when
 * spawned by another process whose binary is in the same .app bundle
 * — macOS LaunchServices treats the child as an "internal helper"
 * of the running parent app and refuses to grant it its own ASN /
 * CGS connection. No CGS connection → no rendering, regardless of
 * Qt window setup.
 *
 * Diagnosed 2026-05-15 via scripts/probe_macos_hud_proc_state.py.
 * The production HUD subprocess showed:
 *
 *     bundleID=[NULL]  pid=!cgsConnection !signalled  LSASN=[NULL]
 *
 * which is "Cocoa initialized but LaunchServices never registered."
 * No CGS connection → no rendering, no matter what.
 *
 * The fix is the canonical Helper.app pattern (Chrome, Electron, Slack
 * all do this): nest a separate .app bundle with a different
 * CFBundleIdentifier inside Frameworks/. macOS treats it as a fully
 * separate app — own ASN, own CGS connection, windows render.
 *
 * Why a wrapper, not a symlink to sayzo-agent
 * --------------------------------------------
 * PyInstaller's bootloader uses _NSGetExecutablePath() to find its
 * bundled _internal/ data. If SayzoHud.app/Contents/MacOS/SayzoHud
 * were a symlink (or copy) of the main sayzo-agent binary, the
 * bootloader would look for its data inside SayzoHud.app/Contents/
 * Frameworks/ — which is empty — and fail to start.
 *
 * This wrapper sidesteps that by being a minimal native program that
 * doesn't init Cocoa. It posix_spawns the real sayzo-agent binary
 * (which lives at /Applications/Sayzo.app/Contents/MacOS/sayzo-agent
 * — the bootloader finds its data normally there) with `hud --idle`
 * args. The wrapper inherits stdin/stdout from the agent (which
 * spawned the wrapper), and the spawned sayzo-agent inherits from
 * the wrapper, so the agent's existing async pipe machinery in
 * gui/hud/launcher.py works unchanged.
 *
 * The Cocoa-initialized HUD process's parent is this wrapper. The
 * wrapper's binary lives at SayzoHud.app/Contents/MacOS/SayzoHud,
 * so its bundle attribution is com.sayzo.agent.hud (different from
 * the running com.sayzo.agent agent). LaunchServices sees the HUD's
 * parent as a com.sayzo.agent.hud process and the HUD itself as a
 * sayzo-agent (com.sayzo.agent) instance — different bundle IDs in
 * the chain → independent registration → CGS connection → renders.
 *
 * What this is NOT doing
 * ----------------------
 * No `responsibility_spawnattrs_setdisclaim` call. We tried that as
 * a separate hack in a prior iteration (see
 * installer/macos/hud_disclaim_spawner.c, kept as historical
 * artifact). It didn't validate. The Helper.app pattern alone is
 * what the OS actually expects for multi-process apps; adding
 * disclaim on top would be belt-and-suspenders that only adds
 * "what does this even do?" mystery for future maintainers.
 *
 * Build
 * =====
 *
 *     cc -O2 -Wall -mmacosx-version-min=11.0 \
 *        -o SayzoHud sayzo_hud_wrapper.c
 *
 * See installer/macos/build_sayzo_hud_wrapper.sh for the convenience
 * wrapper used both locally for validation and in CI.
 *
 * Usage at runtime
 * ----------------
 * Spawned by gui/hud/launcher.py::_spawn_locked on macOS via
 * asyncio.create_subprocess_exec(SayzoHud_path, "hud", "--idle", ...)
 *
 * The wrapper expects argv[1..N] to be the full target argv —
 * launcher.py passes the resolved sayzo-agent binary path as argv[1]
 * and the HUD CLI args (e.g. "hud" "--idle") as the rest.
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;


int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr,
            "usage: %s <sayzo-agent-binary-path> [hud_args...]\n"
            "  Spawns the given binary with `hud --idle` (or any args you pass)\n"
            "  and waits for it. Inherits stdin/stdout/stderr.\n",
            argv[0]);
        return 64;  /* EX_USAGE */
    }

    /*
     * Spawn argv[1..argc-1] as the new process. No file_actions
     * (so the child inherits our stdin/stdout/stderr — that's how
     * the agent's pipes flow through). No spawnattrs (no disclaim,
     * no special process group, nothing — the Helper.app pattern
     * relies entirely on the bundle-id difference for its effect).
     */
    pid_t child_pid;
    int rc = posix_spawn(&child_pid, argv[1], NULL, NULL, &argv[1], environ);
    if (rc != 0) {
        fprintf(stderr,
            "sayzo_hud_wrapper: posix_spawn(%s) failed: %s (%d)\n",
            argv[1], strerror(rc), rc);
        return 66;
    }

    /*
     * Wait for the child and propagate its exit status. EINTR-safe.
     * Killed via SIGTERM/SIGINT from agent's launcher.shutdown():
     * the parent agent first sends "quit" over the child's stdin
     * (which the HUD reads and exits cleanly), then SIGTERM as a
     * fallback. Our waitpid will return on either path.
     */
    int status = 0;
    pid_t waited;
    do {
        waited = waitpid(child_pid, &status, 0);
    } while (waited == -1 && errno == EINTR);

    if (waited == -1) {
        fprintf(stderr,
            "sayzo_hud_wrapper: waitpid(%d) failed: %s\n",
            child_pid, strerror(errno));
        return 67;
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 69;
}
