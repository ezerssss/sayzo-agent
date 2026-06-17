"""Probe: spawn the HUD with `responsibility_spawnattrs_setdisclaim` set.

Background — the v3.1.6 → v3.1.7 root cause
--------------------------------------------
Section 3 of the proc-state diff between B (Finder-launched agent's
HUD subprocess, INVISIBLE) and C (terminal-spawn HUD with Finder agent
also running, VISIBLE) showed:

    B: bundleID=[NULL]  pid=!cgsConnection !signalled  LSASN=[NULL]
    C: bundleID="com.sayzo.agent"  pid=<n> type="UIElement"  LSASN=ASN:0x0-0x1d91d9

B has NO LaunchServices registration and NO CGS (Core Graphics System)
connection — that's why nothing renders. C has both.

The literal trigger: when ``posix_spawn`` is called by a process whose
binary is in the SAME bundle as the spawn target (e.g. agent at
``/Applications/Sayzo.app/Contents/MacOS/sayzo-agent`` spawning the
HUD at the same path), macOS LaunchServices classifies the child as
an "internal helper" of the already-running parent app and does NOT
give it an independent ASN / CGS connection.

The Apple-private API ``responsibility_spawnattrs_setdisclaim(attrs, 1)``
tells the spawn machinery "this child is responsible for itself, not
its parent." With self-responsibility, LaunchServices registers the
child as an independent app instance — own ASN, own CGS connection,
windows render.

This is the same technique Sparkle uses for spawning auto-updaters and
that App Store updates use under the hood.

What this probe does
--------------------
1. Loads ``/usr/lib/libSystem.B.dylib`` via ctypes
2. Initializes ``posix_spawnattr_t``
3. Calls ``responsibility_spawnattrs_setdisclaim(attrs, 1)`` (or skips
   it if ``--no-disclaim`` for control comparison)
4. Calls ``posix_spawn`` to launch the HUD binary
5. Waits 3 seconds for Cocoa to initialize, then dumps the resulting
   HUD subprocess's ``lsappinfo`` state
6. Reports whether the HUD got CGS connection + bundle registration

Local validation (prior to patching launcher.py)
------------------------------------------------
A) From a regular Terminal (parent = zsh)::

       python3 scripts/probe_disclaim_spawn.py
       python3 scripts/probe_disclaim_spawn.py --no-disclaim   # control

   Both should show CGS connection because the parent (python3) is a
   different bundle from Sayzo. This tests that the ctypes invocation
   itself works without errors and that we can spawn the HUD via
   posix_spawn.

B) For the production-like test, run inside the agent's bootstrap
   context via ``launchctl bsexec``::

       killall -9 sayzo-agent 2>/dev/null; sleep 1
       open /Applications/Sayzo.app && sleep 5
       AGENT_PID=$(pgrep sayzo-agent | head -1)

       # Disclaim ON — should produce a HUD that has CGS connection
       sudo launchctl bsexec $AGENT_PID python3 scripts/probe_disclaim_spawn.py

       # Control: Disclaim OFF — closer to what current launcher.py does
       sudo launchctl bsexec $AGENT_PID python3 scripts/probe_disclaim_spawn.py --no-disclaim

   Even though the immediate parent here is python3 (not sayzo-agent),
   the bsexec puts the python3 process in the agent's launchd bootstrap
   context. This is the closest local proxy for the production scenario.

What success looks like
-----------------------
Output line ``Has CGS connection: True`` and ``Has bundleID populated: True``
when ``--disclaim`` is on. If those are True with disclaim and False
without, we have proof the disclaim attribute fixes the registration
gap, and we can confidently patch launcher.py with the same approach.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_char_p, c_int, c_void_p
from typing import Optional

LIBSYSTEM_PATH = "/usr/lib/libSystem.B.dylib"


def _load_libsystem() -> ctypes.CDLL:
    """Load libSystem and configure ctypes signatures for the calls we need."""
    lib = ctypes.CDLL(LIBSYSTEM_PATH)

    # int posix_spawnattr_init(posix_spawnattr_t *attr);
    # int posix_spawnattr_destroy(posix_spawnattr_t *attr);
    lib.posix_spawnattr_init.argtypes = [POINTER(c_void_p)]
    lib.posix_spawnattr_init.restype = c_int
    lib.posix_spawnattr_destroy.argtypes = [POINTER(c_void_p)]
    lib.posix_spawnattr_destroy.restype = c_int

    # Apple-private API. Not in <spawn.h> public headers but exported
    # from libsystem. Used by Sparkle, App Store, etc.
    #
    #   int responsibility_spawnattrs_setdisclaim(
    #       posix_spawnattr_t attrs, int disclaim);
    #
    # NOTE: takes attrs BY VALUE (which is itself a pointer since
    # ``posix_spawnattr_t`` is ``typedef void * posix_spawnattr_t`` on
    # Darwin) — NOT by pointer-to-pointer like init/destroy.
    lib.responsibility_spawnattrs_setdisclaim.argtypes = [c_void_p, c_int]
    lib.responsibility_spawnattrs_setdisclaim.restype = c_int

    # int posix_spawn(pid_t *pid, const char *path,
    #                 const posix_spawn_file_actions_t *file_actions,
    #                 const posix_spawnattr_t *attrp,
    #                 char *const argv[], char *const envp[]);
    lib.posix_spawn.argtypes = [
        POINTER(c_int),
        c_char_p,
        c_void_p,
        POINTER(c_void_p),
        POINTER(c_char_p),
        POINTER(c_char_p),
    ]
    lib.posix_spawn.restype = c_int

    return lib


def spawn_with_disclaim(
    binary: str,
    argv: list[str],
    *,
    disclaim: bool,
    env: Optional[dict[str, str]] = None,
) -> int:
    """Spawn ``binary`` with given argv via posix_spawn; optionally disclaim.

    Returns child PID on success, raises OSError on failure.
    """
    lib = _load_libsystem()

    # Allocate posix_spawnattr_t (which is a pointer — c_void_p slot).
    attrs = c_void_p()
    rc = lib.posix_spawnattr_init(byref(attrs))
    if rc != 0:
        raise OSError(rc, f"posix_spawnattr_init failed (rc={rc})")

    try:
        if disclaim:
            rc = lib.responsibility_spawnattrs_setdisclaim(attrs, 1)
            if rc != 0:
                raise OSError(
                    rc,
                    f"responsibility_spawnattrs_setdisclaim failed (rc={rc}) — "
                    f"may not be available on this macOS version",
                )

        # Build argv as null-terminated array of c_char_p. Hold the
        # bytes objects in a list so Python's GC doesn't free them
        # while ctypes holds raw pointers.
        argv_bytes_holder: list[Optional[bytes]] = (
            [binary.encode()] + [a.encode() for a in argv] + [None]
        )
        argv_array = (c_char_p * len(argv_bytes_holder))(*argv_bytes_holder)

        # Same for envp — pass current env unless overridden.
        env_dict = env if env is not None else dict(os.environ)
        env_bytes_holder: list[Optional[bytes]] = [
            f"{k}={v}".encode() for k, v in env_dict.items()
        ] + [None]
        envp_array = (c_char_p * len(env_bytes_holder))(*env_bytes_holder)

        pid_var = c_int(0)
        rc = lib.posix_spawn(
            byref(pid_var),
            binary.encode(),
            None,            # no file_actions
            byref(attrs),
            argv_array,
            envp_array,
        )
        if rc != 0:
            raise OSError(rc, f"posix_spawn failed (rc={rc})")

        return pid_var.value
    finally:
        lib.posix_spawnattr_destroy(byref(attrs))


def query_lsappinfo(pid: int) -> str:
    result = subprocess.run(
        ["lsappinfo", "info", str(pid)],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Spawn HUD via posix_spawn with optional disclaim flag.",
    )
    p.add_argument(
        "--no-disclaim",
        action="store_true",
        help="Don't set the disclaim flag (control comparison)",
    )
    p.add_argument(
        "--binary",
        default="/Applications/Sayzo.app/Contents/MacOS/sayzo-agent",
        help="Binary to spawn",
    )
    p.add_argument(
        "--args",
        nargs="*",
        default=["hud", "--demo"],
        help="Args to pass after binary (default: hud --demo)",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Seconds to keep child alive before killing (default: 15)",
    )
    args = p.parse_args(argv)

    disclaim = not args.no_disclaim

    print("=" * 72)
    print(f"DISCLAIM PROBE — disclaim={disclaim}")
    print("=" * 72)
    print(f"Binary:     {args.binary}")
    print(f"Args:       {args.args}")
    print(f"Probe PID:  {os.getpid()}")
    print(f"Probe PPID: {os.getppid()}")
    print()

    try:
        child_pid = spawn_with_disclaim(args.binary, args.args, disclaim=disclaim)
    except OSError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"Spawned child PID: {child_pid}")
    print("Sleeping 3s for Cocoa initialization...")
    time.sleep(3)

    # Check if process is still alive
    try:
        os.kill(child_pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False

    if not alive:
        print(f"WARN: child PID {child_pid} no longer alive at probe time")

    print()
    print("=" * 72)
    print(f"lsappinfo info {child_pid}")
    print("=" * 72)
    info = query_lsappinfo(child_pid)
    print(info)

    # Heuristic checks
    has_cgs = ("!cgsConnection" not in info) and ("!signalled" not in info)
    has_bundle_id = '"com.sayzo.agent"' in info or 'bundleID="com.sayzo.agent"' in info
    is_null = "[ NULL ]" in info and "bundleID=[ NULL ]" in info

    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    print(f"disclaim flag was set:        {disclaim}")
    print(f"Process still alive:          {alive}")
    print(f"Has CGS connection:           {has_cgs}")
    print(f"Has bundleID populated:       {has_bundle_id}")
    print(f"All registration fields NULL: {is_null}")
    print()
    if has_cgs and has_bundle_id and not is_null:
        print("✓ HUD is properly registered with LaunchServices + CGS.")
        print("  → If this matches expected outcome for the disclaim flag setting,")
        print("    the disclaim mechanism works and we should patch launcher.py.")
    elif is_null:
        print("✗ HUD has NO LaunchServices registration / NO CGS connection.")
        print("  → This matches the production bug. If this happened with disclaim=True,")
        print("    the disclaim mechanism alone is NOT sufficient — fall back to Helper.app.")
    else:
        print("? Mixed state — inspect lsappinfo output above carefully.")

    print()
    print(f"Sleeping {args.wait}s — watch top-right of screen for HUD demo content.")
    print("Ctrl+C to quit early.")
    try:
        time.sleep(args.wait)
    except KeyboardInterrupt:
        pass

    try:
        os.kill(child_pid, 9)
    except ProcessLookupError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
