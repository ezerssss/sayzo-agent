#!/usr/bin/env python3
"""Probe 2 — Per-process audio attribution via macOS 14.4+ APIs.

THIS IS THE KEY EXPERIMENT.

The current agent claims macOS can't tell us *which* process is using the
mic. That's only true on macOS < 14.4. Since 14.4 (which we already
require for the system-audio capture path) CoreAudio exposes:

  kAudioHardwarePropertyProcessObjectList   → list of AudioObjectIDs,
                                                one per process that has
                                                ever produced/consumed
                                                audio in this session
  kAudioProcessPropertyPID                  → pid_t for that process
  kAudioProcessPropertyBundleID             → CFString bundle id
  kAudioProcessPropertyIsRunning            → bool: any IO active?
  kAudioProcessPropertyIsRunningInput       → bool: capturing mic?  ← THE ONE WE WANT
  kAudioProcessPropertyIsRunningOutput      → bool: playing audio?

If ``IsRunningInput`` flips to True for ``us.zoom.xos`` when you join a
Zoom call, the entire macOS detection rewrite becomes "do exactly what
Windows does — match on direct mic-holder process names". No more
foreground requirement, no more browser-foreground requirement, no more
AppleScript prompts for tab URLs.

What you should see:
- Idle, no apps using audio → table is empty or shows only system bundles
  with input=no / output=no.
- Open Zoom and join a call → row appears with bundle_id=us.zoom.xos and
  is_running_input=YES.
- Same for Discord (com.hnc.Discord), Meet (Chrome's bundle), Teams
  (com.microsoft.teams2), etc.

If is_running_input does NOT flip to YES on the meeting app, that's the
critical finding — try ``--show-all`` to see every process and look for
something unexpected (e.g. a CoreAudio aggregate device PID rather than
the meeting app itself).

Usage:
    python3 02_audio_processes.py                # one-shot, show only
                                                 #  is_running_input=YES rows
    python3 02_audio_processes.py --show-all     # show every audio process
    python3 02_audio_processes.py --watch        # poll every 1 s
"""
from __future__ import annotations

import argparse
import sys
import time

from _common import fmt_bool, snapshot_audio_processes


# When ``--enrich`` is set, also resolve PID → process name via psutil
# for the rare case the bundle id read returned None (system processes,
# helpers without a bundle id). Optional because psutil is an extra dep.
def _enrich_with_proc_name(rows: list[dict]) -> None:
    try:
        import psutil
    except ImportError:
        return
    for r in rows:
        pid = r.get("pid")
        if pid is None or pid <= 0:
            continue
        try:
            r["proc_name"] = psutil.Process(pid).name()
        except Exception:
            r["proc_name"] = None


def _print_table(rows: list[dict], show_all: bool) -> None:
    if not show_all:
        rows = [r for r in rows if r.get("is_running_input") == 1]
    if not rows:
        print("  (no audio processes match — try --show-all to see every "
              "registered process)")
        return

    header = f"  {'PID':>6}  {'in':>3}  {'out':>3}  {'run':>3}  bundle_id / proc"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        pid = r["pid"] if r["pid"] is not None else "-"
        bid = r.get("bundle_id") or "-"
        proc = r.get("proc_name")
        label = bid if bid != "-" else f"<no bundle> ({proc})" if proc else "<no bundle>"
        if proc and bid != "-":
            label = f"{bid}  [{proc}]"
        print(
            f"  {str(pid):>6}  "
            f"{fmt_bool(r['is_running_input']):>3}  "
            f"{fmt_bool(r['is_running_output']):>3}  "
            f"{fmt_bool(r['is_running']):>3}  "
            f"{label}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show-all", action="store_true",
                    help="Print every audio process, not just is_running_input=YES")
    ap.add_argument("--watch", action="store_true",
                    help="Re-snapshot every --interval seconds until Ctrl-C")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip psutil-based proc-name lookup")
    args = ap.parse_args()

    def one_pass() -> None:
        try:
            rows = snapshot_audio_processes()
        except OSError as exc:
            print(f"\n  ERROR enumerating ProcessObjectList: {exc}")
            print("\n  Common OSStatus values:")
            print("    2003332927 = 'who?' = kAudioHardwareUnknownPropertyError")
            print("       → property selector not recognized. Either macOS < 14.4")
            print("         or (on macOS 14.4+) the API is gated behind an entitlement")
            print("         or Info.plist key that an unsigned Python script lacks.")
            print("    Run: sw_vers -productVersion")
            return
        if not args.no_enrich:
            _enrich_with_proc_name(rows)
        _print_table(rows, args.show_all)

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        one_pass()
        return

    print("Watching audio processes. Ctrl-C to stop.")
    print("Try: join Zoom / Meet / Discord / Teams. The row should appear with input=YES.\n")
    try:
        while True:
            print(f"\n[{time.strftime('%H:%M:%S')}]")
            one_pass()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
