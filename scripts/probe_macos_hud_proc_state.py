"""Probe: capture full macOS process state for diff-based HUD-invisibility root-cause isolation.

Background — the v3.1.6 mystery
-------------------------------
v3.1.5 fixed the V8/JIT entitlement crash on Apple Silicon. v3.1.6 added
``_hud_subprocess_env()`` to scrub ``__CFBundleIdentifier``,
``XPC_SERVICE_NAME``, and ``XPC_FLAGS`` from the HUD subprocess env.
Local A/B testing suggested the env scrub would fix it: spawning a HUD
subprocess from terminal (no LaunchServices env vars) while a
Finder-launched agent (full LaunchServices env vars) was also running →
HUD VISIBLE. But after installing v3.1.6 in production, the HUD is
STILL invisible when the agent is Finder-launched.

So either (a) v3.1.6's env scrub isn't actually being applied at runtime
in the shipped PyInstaller bundle, or (b) something is inherited beyond
env vars when the subprocess is spawned by the agent — most likely the
**Mach bootstrap port** (which ``posix_spawn`` propagates regardless of
``env=``) and/or the **LaunchServices ASN** that flows from it.

This probe captures every dimension of macOS process state for a given
PID so we can diff working vs. broken HUD-subprocess scenarios and pin
down the literal trigger.

Dimensions captured
-------------------
1. Process tree (PPID, PGID, SID, flags, command)
2. Environment variables (filtered to LaunchServices / Qt / locale keys)
3. LaunchServices info (lsappinfo) — ASN, bundle attribution, app state
4. launchd domain membership (launchctl print pid/N)
5. launchd process info (launchctl procinfo) — includes bootstrap port name
6. Activation policy (NSRunningApplication via PyObjC)
7. Mach ports (lsmp — requires sudo for other-user processes)
8. Open file descriptors (lsof, first 25 lines)

Usage
-----
On the Mac::

    # Direct query — works for own-user processes for most dimensions.
    # ``lsmp`` may need sudo; gracefully degrades if denied.
    python3 scripts/probe_macos_hud_proc_state.py --pid <PID>

For Mach-port-level detail (recommended for definitive diff)::

    sudo python3 scripts/probe_macos_hud_proc_state.py --pid <PID>

Diff workflow (the actual point of this script)
-----------------------------------------------
Capture state in three scenarios; diff them::

    # Scenario A — terminal-spawn HUD solo (KNOWN VISIBLE)
    killall -9 sayzo-agent 2>/dev/null; sleep 1
    /Applications/Sayzo.app/Contents/MacOS/sayzo-agent hud --demo &
    HUD_PID=$!
    sleep 3
    sudo python3 scripts/probe_macos_hud_proc_state.py --pid $HUD_PID > ~/Downloads/proc_A.txt
    kill $HUD_PID 2>/dev/null

    # Scenario B — production HUD with Finder-launched agent (KNOWN INVISIBLE)
    killall -9 sayzo-agent 2>/dev/null; sleep 1
    open /Applications/Sayzo.app
    sleep 5
    HUD_PID=$(ps -axww -o pid,command | awk '/sayzo-agent hud/ {print $1; exit}')
    sudo python3 scripts/probe_macos_hud_proc_state.py --pid $HUD_PID > ~/Downloads/proc_B.txt
    killall -9 sayzo-agent

    # Scenario C — terminal-spawn HUD WHILE Finder-launched agent runs (KNOWN VISIBLE)
    killall -9 sayzo-agent 2>/dev/null; sleep 1
    open /Applications/Sayzo.app
    sleep 5
    /Applications/Sayzo.app/Contents/MacOS/sayzo-agent hud --demo &
    HUD_PID=$!
    sleep 3
    sudo python3 scripts/probe_macos_hud_proc_state.py --pid $HUD_PID > ~/Downloads/proc_C.txt
    kill $HUD_PID 2>/dev/null
    killall -9 sayzo-agent

    # The decisive diff — both have a Finder-launched agent running;
    # only the HUD spawn mechanism differs. Whatever differs IS the trigger.
    diff ~/Downloads/proc_B.txt ~/Downloads/proc_C.txt | tee ~/Downloads/diff_B_vs_C.txt

What we're looking for in the diff
----------------------------------
* A different ``Mach bootstrap port`` value → bootstrap port inheritance
  is the trigger. Fix: detach via Mach API in HUD subprocess startup.
* A different ``LaunchServices ASN`` → ASN sharing is the trigger. Fix:
  force a new ASN via private LaunchServices API.
* A different ``launchd domain`` line → launchd job-context inheritance.
  Fix: spawn HUD via mechanism that switches launchd domain.
* Only env vars differ (we already strip 3 — so a 4th was missed) →
  add it to ``_hud_subprocess_env()``'s pop loop.
* ``activationPolicy`` differs → app-level classification difference.
* Multiple things differ → fix bootstrap port first; usually upstream of
  ASN and activation policy.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import Optional


_LAUNCHSERVICES_ENV_PREFIXES = (
    "__CF",
    "XPC_",
    "LS",
    "Apple_PubSub",
    "MallocSpace",
    "CFFIXED",
    "CFLOG",
    "CFNETWORK",
    "CARBON_",
    "SECURITYSESSIONID",
)
_GENERAL_ENV_PREFIXES = (
    "SHELL=",
    "USER=",
    "HOME=",
    "TMPDIR=",
    "TERM=",
    "PYTHONPATH=",
    "QTWEBENGINE_",
    "QT_LOGGING_RULES",
    "QT_QPA_",
    "QT_PLUGIN_PATH",
)


def _run_cmd(*args: str, timeout: float = 10.0) -> str:
    """Run a command, return stdout (with stderr appended if rc != 0)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
        out = result.stdout
        if result.returncode != 0 and result.stderr:
            out += f"\n[stderr rc={result.returncode}] {result.stderr}"
        return out.strip()
    except FileNotFoundError as e:
        return f"[ERROR command not found] {e}"
    except subprocess.TimeoutExpired:
        return f"[ERROR timeout after {timeout}s] {' '.join(args)}"
    except Exception as e:
        return f"[ERROR running {args}] {type(e).__name__}: {e}"


def _section(title: str) -> str:
    return f"\n{'=' * 72}\n{title}\n{'=' * 72}"


def capture_process_tree(pid: int) -> str:
    """ps with all the process-isolation columns."""
    return _run_cmd(
        "ps", "-o", "pid,ppid,pgid,sid,user,uid,flags,wq,nwq,command", "-p", str(pid),
    )


def capture_env(pid: int) -> str:
    """`ps eww` filtered to LaunchServices / Qt / locale keys.

    The full env dump can be huge (hundreds of vars from PATH and
    PyInstaller bundling), so we filter to the keys that matter for
    this debug.
    """
    raw = _run_cmd("ps", "eww", str(pid))
    matches = []
    for token in raw.split():
        if "=" not in token:
            continue
        if any(token.startswith(p) for p in _LAUNCHSERVICES_ENV_PREFIXES):
            matches.append(token)
        elif any(token.startswith(p) for p in _GENERAL_ENV_PREFIXES):
            matches.append(token)
    if not matches:
        return "(no matching env vars found — process may have exited)"
    return "\n".join(sorted(matches))


def capture_lsappinfo(pid: int) -> str:
    """LaunchServices ASN + full bundle/app info for the PID."""
    asn = _run_cmd("lsappinfo", "info", "-only", "ASN", str(pid))
    full = _run_cmd("lsappinfo", "info", str(pid))
    return f"--- ASN ONLY ---\n{asn}\n\n--- FULL INFO ---\n{full}"


def capture_launchctl_print(pid: int) -> str:
    """`launchctl print pid/N` — which launchd domain the PID lives in."""
    return _run_cmd("launchctl", "print", f"pid/{pid}", timeout=15.0)


def capture_launchctl_procinfo(pid: int) -> str:
    """`launchctl procinfo N` — process info including Mach bootstrap port name.

    Goldmine output: shows bootstrap port name, host port, audit token,
    launchd job association. This is the cleanest non-ctypes way to get
    Mach bootstrap state.
    """
    return _run_cmd("launchctl", "procinfo", str(pid), timeout=15.0)


def capture_lsmp(pid: int) -> str:
    """`lsmp -p N` — list Mach ports for the PID.

    Usually requires sudo for other-user processes; for own-user
    processes this often works without elevation. If it fails, the
    output is still useful as a marker.
    """
    raw = _run_cmd("lsmp", "-p", str(pid), timeout=15.0)
    # Truncate to first 60 lines — full lsmp output can be hundreds of lines
    lines = raw.splitlines()
    head = "\n".join(lines[:60])
    if len(lines) > 60:
        head += f"\n... ({len(lines) - 60} more lines truncated)"
    return head


def capture_activation_policy(pid: int) -> str:
    """NSRunningApplication.runningApplicationWithProcessIdentifier_."""
    try:
        from AppKit import NSRunningApplication  # type: ignore[import-not-found]
    except Exception as e:
        return (
            f"[ERROR pyobjc-framework-Cocoa not installed] {e}\n"
            f"Run: python3 -m pip install --user pyobjc-framework-Cocoa"
        )

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return (
            f"NSRunningApplication.runningApplicationWithProcessIdentifier_({pid}) "
            f"returned nil — process may not be a UI app or LaunchServices "
            f"hasn't registered it yet"
        )

    policy = int(app.activationPolicy())
    policy_name = {0: "Regular", 1: "Accessory", 2: "Prohibited"}.get(
        policy, f"Unknown({policy})"
    )
    return (
        f"bundleIdentifier: {app.bundleIdentifier()}\n"
        f"localizedName:    {app.localizedName()}\n"
        f"activationPolicy: {policy_name} ({policy})\n"
        f"isHidden:         {bool(app.isHidden())}\n"
        f"isActive:         {bool(app.isActive())}\n"
        f"isFinishedLaunching: {bool(app.isFinishedLaunching())}\n"
        f"ownsMenuBar:      {bool(app.ownsMenuBar())}\n"
        f"executableURL:    {app.executableURL()}\n"
        f"bundleURL:        {app.bundleURL()}\n"
        f"processIdentifier: {app.processIdentifier()}\n"
        f"launchDate:       {app.launchDate()}\n"
    )


def capture_lsof(pid: int) -> str:
    """`lsof -p N` — first 25 file descriptors. Reveals controlling tty / pipes."""
    raw = _run_cmd("lsof", "-p", str(pid), timeout=15.0)
    lines = raw.splitlines()
    head = "\n".join(lines[:25])
    if len(lines) > 25:
        head += f"\n... ({len(lines) - 25} more fds truncated)"
    return head


def capture_codesign(pid: int) -> str:
    """`codesign -d --entitlements - <binary>` — confirms the binary's
    entitlements (which propagate to all running instances of it).

    Only useful as a sanity check — entitlements are static per-binary,
    not per-process. But if the bundle was re-signed locally with
    different entitlements, that would show here.
    """
    # Get the executable path from ps
    raw = _run_cmd("ps", "-o", "comm=", "-p", str(pid))
    if not raw or raw.startswith("[ERROR"):
        return "could not resolve binary path"
    return _run_cmd("codesign", "-d", "--entitlements", "-", raw)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Capture macOS process state for diff-based HUD-invisibility root-cause isolation.",
    )
    p.add_argument("--pid", type=int, required=True, help="Target process PID")
    p.add_argument(
        "--no-codesign",
        action="store_true",
        help="Skip codesign entitlements dump (usually identical for the same binary)",
    )
    args = p.parse_args()
    pid = args.pid

    # Verify PID exists before doing anything else
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"ERROR: PID {pid} does not exist", file=sys.stderr)
        return 2
    except PermissionError:
        # Process exists but we can't signal it — still try to query
        pass

    print(f"# macOS process state capture")
    print(f"# Target PID: {pid}")
    print(f"# Captured at: {_run_cmd('date')}")
    print(f"# Probe pid: {os.getpid()}  uid: {os.getuid()}  euid: {os.geteuid()}")
    print(f"# (Note: lsmp + some launchctl details may need sudo for full output.)")

    print(_section("1. PROCESS TREE (ps -o pid,ppid,pgid,sid,...)"))
    print(capture_process_tree(pid))

    print(_section(
        "2. ENVIRONMENT VARS (filtered to LaunchServices / CF / XPC / Qt / locale)"
    ))
    print(capture_env(pid))

    print(_section("3. LAUNCHSERVICES INFO (lsappinfo)"))
    print(capture_lsappinfo(pid))

    print(_section("4. LAUNCHD DOMAIN (launchctl print pid/N)"))
    print(capture_launchctl_print(pid))

    print(_section(
        "5. LAUNCHD PROCESS INFO (launchctl procinfo N)\n"
        "   — includes Mach bootstrap port, audit token, host special ports"
    ))
    print(capture_launchctl_procinfo(pid))

    print(_section("6. ACTIVATION POLICY (NSRunningApplication via PyObjC)"))
    print(capture_activation_policy(pid))

    print(_section("7. MACH PORTS (lsmp -p N — may need sudo)"))
    print(capture_lsmp(pid))

    print(_section("8. OPEN FILE DESCRIPTORS (lsof, first 25)"))
    print(capture_lsof(pid))

    if not args.no_codesign:
        print(_section("9. CODESIGN ENTITLEMENTS (sanity check — should match bundle's)"))
        print(capture_codesign(pid))

    return 0


if __name__ == "__main__":
    sys.exit(main())
