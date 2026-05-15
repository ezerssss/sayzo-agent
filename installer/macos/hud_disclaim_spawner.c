/*
 * hud_disclaim_spawner.c — macOS-only tiny wrapper that spawns its
 * argv[1..N] target via posix_spawn with
 * `responsibility_spawnattrs_setdisclaim(attrs, 1)` set.
 *
 * Why this exists
 * ===============
 *
 * The Sayzo HUD subprocess (PySide6 + QtWebEngine) renders invisibly
 * when spawned by a parent process whose binary is in the SAME .app
 * bundle (i.e. agent and HUD are both
 * /Applications/Sayzo.app/Contents/MacOS/sayzo-agent under different
 * argv). Diagnosed 2026-05-15 via
 * scripts/probe_macos_hud_proc_state.py — the production HUD has
 *
 *     bundleID=[NULL]  pid=!cgsConnection !signalled  LSASN=[NULL]
 *
 * which means macOS LaunchServices treats the HUD as an "internal
 * helper of the already-registered Sayzo bundle parent" and refuses
 * to give it its own ASN / Core Graphics System (window server)
 * connection. No CGS connection → no rendering, no matter what the
 * Qt window code does.
 *
 * The Apple-private API
 * `responsibility_spawnattrs_setdisclaim(attrs, 1)` tells the spawn
 * machinery "the spawned child is responsible for itself, not its
 * parent." With self-responsibility, LaunchServices registers the
 * child as an independent app with its own ASN + CGS connection.
 *
 * This is the same technique Sparkle, App Store auto-update, and many
 * other macOS multi-process apps use. We keep it as a native binary
 * (rather than a Python ctypes call inside launcher.py) so that:
 *
 *   1. The wrapper itself sits inside Sayzo.app/Contents/MacOS/, so
 *      it inherits the bundle's codesign + entitlements. The HUD
 *      child it spawns also gets bundle attribution from this path.
 *
 *   2. We avoid Python's posix_spawn / asyncio plumbing complexity in
 *      launcher.py — the wrapper is a 50-line program that does one
 *      thing, and launcher.py just shells out to it via the existing
 *      `asyncio.create_subprocess_exec` path.
 *
 *   3. It's locally testable on a Mac with the system clang (no
 *      Python rebuild required) — see the test recipe in
 *      installer/macos/build_hud_disclaim_spawner.sh.
 *
 * Usage
 * =====
 *
 *     hud_disclaim_spawner <child_binary> [child_arg]...
 *
 * The wrapper inherits stdin/stdout/stderr from its parent and passes
 * them to the spawned child via FD inheritance (no file_actions). The
 * wrapper waits for the child to exit and propagates the exit status,
 * so it appears as a transparent stand-in to the parent process tree.
 *
 * Build
 * =====
 *
 *     cc -O2 -Wall -o hud_disclaim_spawner hud_disclaim_spawner.c
 *
 * Or via the helper script: installer/macos/build_hud_disclaim_spawner.sh
 *
 * Don't statically link or do anything fancy — the symbol
 * responsibility_spawnattrs_setdisclaim is in libSystem (loaded by
 * default on macOS), so a default link is enough.
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

/*
 * Apple-private declaration (not in <spawn.h> public headers, but
 * exported from /usr/lib/libSystem.B.dylib). Apple's own apps and
 * frameworks like Sparkle declare it the same way.
 */
extern int responsibility_spawnattrs_setdisclaim(
    posix_spawnattr_t attrs, int disclaim);


int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr,
            "usage: %s <child_binary> [child_arg]...\n"
            "  Spawns the child via posix_spawn with "
            "responsibility_spawnattrs_setdisclaim(attrs, 1) set, then\n"
            "  waits for the child to exit and propagates its exit status.\n",
            argv[0]);
        return 64;  /* EX_USAGE */
    }

    posix_spawnattr_t attrs;
    int rc = posix_spawnattr_init(&attrs);
    if (rc != 0) {
        fprintf(stderr,
            "hud_disclaim_spawner: posix_spawnattr_init failed: %s\n",
            strerror(rc));
        return 65;
    }

    /*
     * The actual fix: tell macOS this child is responsible for itself,
     * not its parent. Without this, LaunchServices classifies the
     * child as "internal helper of the already-registered Sayzo
     * bundle" and skips ASN / CGS registration.
     */
    rc = responsibility_spawnattrs_setdisclaim(attrs, 1);
    if (rc != 0) {
        fprintf(stderr,
            "hud_disclaim_spawner: responsibility_spawnattrs_setdisclaim "
            "failed: %s — this macOS version may not support it; falling "
            "back to plain spawn (HUD may stay invisible)\n",
            strerror(rc));
        /*
         * Don't bail — fall through to a plain spawn so we degrade
         * gracefully rather than blocking startup entirely.
         */
    }

    pid_t child_pid;
    /*
     * Pass &argv[1] as the child's argv (so child sees its own binary
     * path as argv[0] like a normal exec). Pass `environ` so the
     * child inherits the wrapper's full environment, which the agent
     * has already scrubbed via _hud_subprocess_env() on its end.
     */
    rc = posix_spawn(&child_pid, argv[1], NULL, &attrs, &argv[1], environ);
    posix_spawnattr_destroy(&attrs);

    if (rc != 0) {
        fprintf(stderr,
            "hud_disclaim_spawner: posix_spawn(%s) failed: %s\n",
            argv[1], strerror(rc));
        return 66;
    }

    /*
     * Forward common termination signals to the child so the parent
     * agent can shut us down cleanly via SIGTERM / SIGINT. We don't
     * trap SIGKILL (impossible) or SIGSTOP (also impossible), but
     * those wouldn't reach the child anyway via our process.
     */
    /* No signal forwarding loop here — keep it dead simple. The
     * agent's launcher writes "quit" over stdin which the HUD reads
     * directly. If the wrapper receives SIGTERM, we'll fall out of
     * waitpid and propagate it. */

    int status = 0;
    pid_t waited;
    do {
        waited = waitpid(child_pid, &status, 0);
    } while (waited == -1 && errno == EINTR);

    if (waited == -1) {
        fprintf(stderr,
            "hud_disclaim_spawner: waitpid(%d) failed: %s\n",
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
