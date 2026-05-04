#!/usr/bin/env python3
"""Probe 6 — Enumerate every audio INPUT device, check IsRunningSomewhere
on each.

Probe 1 found that ``IsRunningSomewhere`` on the *default input* device
stays False even when Zoom is in a call. Most likely cause: Zoom (or
whichever app) is recording from a NON-default input device. macOS lets
each app pick its own input independently of the system default — Zoom
might be on AirPods while the system default is the built-in mic.

This probe queries EVERY audio device that has at least one input stream,
shows you which is the system default, and prints the active-state bits
for each. If a non-default device flips to ``IsRunningSomewhere=YES``
when you join a call, the fix in the agent is small: instead of querying
just the default input, scan all input devices.

What you should see:
  - Idle: every device has ``RunningSomewhere=no``.
  - Join Zoom call: ONE device flips to ``RunningSomewhere=YES``. Note
    which one — its name, transport (built-in / bluetooth / virtual / ...),
    and whether it's the system default.

If NOTHING flips on any device during a real call, then the per-device
bit is broken on this macOS version, and we'll need a different signal
(probe 7 will explore alternatives — e.g. a tiny signed Swift helper
with the right entitlements).

Hog mode: ``hog_pid`` is the PID of the process holding exclusive access
to the device, or -1 if none. Most apps don't take exclusive — Zoom and
Meet typically don't — so this is usually -1 even mid-call. Included
because some pro audio apps DO take exclusive and that'd give us a
direct PID-to-device mapping.

Usage:
    python3 06_all_input_devices.py
    python3 06_all_input_devices.py --watch
    python3 06_all_input_devices.py --include-output    # also list outputs
"""
from __future__ import annotations

import argparse
import sys
import time

from _common import (
    device_has_streams_in_scope,
    get_default_input_device_id,
    kAudioDevicePropertyDeviceIsRunning,
    kAudioDevicePropertyDeviceIsRunningSomewhere,
    kAudioDevicePropertyDeviceUID,
    kAudioDevicePropertyHogMode,
    kAudioDevicePropertyTransportType,
    kAudioObjectPropertyName,
    kAudioObjectPropertyScopeGlobal,
    kAudioObjectPropertyScopeInput,
    kAudioObjectPropertyScopeOutput,
    list_audio_devices,
    read_cfstring_with_scope,
    read_int32_with_scope,
    read_uint32_with_scope,
    transport_label,
)


def _device_summary(dev_id: int) -> dict:
    name = read_cfstring_with_scope(
        dev_id, kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal
    )
    uid = read_cfstring_with_scope(
        dev_id, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal
    )
    transport = read_uint32_with_scope(
        dev_id, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal
    )
    has_in = device_has_streams_in_scope(dev_id, kAudioObjectPropertyScopeInput)
    has_out = device_has_streams_in_scope(dev_id, kAudioObjectPropertyScopeOutput)
    # ScopeInput-specific active reads — different scope can return
    # different values on devices that have both directions.
    running_some_in = read_uint32_with_scope(
        dev_id, kAudioDevicePropertyDeviceIsRunningSomewhere,
        kAudioObjectPropertyScopeInput,
    )
    running_in = read_uint32_with_scope(
        dev_id, kAudioDevicePropertyDeviceIsRunning,
        kAudioObjectPropertyScopeInput,
    )
    # Global-scope read as a fallback; some devices only respond on global.
    running_some_global = read_uint32_with_scope(
        dev_id, kAudioDevicePropertyDeviceIsRunningSomewhere,
        kAudioObjectPropertyScopeGlobal,
    )
    hog_pid = read_int32_with_scope(
        dev_id, kAudioDevicePropertyHogMode, kAudioObjectPropertyScopeGlobal
    )
    return {
        "id": dev_id,
        "name": name,
        "uid": uid,
        "transport": transport_label(transport),
        "has_input": has_in,
        "has_output": has_out,
        "running_somewhere_input": running_some_in,
        "running_somewhere_global": running_some_global,
        "running_input": running_in,
        "hog_pid": hog_pid,
    }


def _bool_cell(val) -> str:
    if val is None:
        return " ?"
    if val == 0:
        return "no"
    if val == 1:
        return "**YES**"
    return str(val)


def _print_one_pass(include_output: bool) -> None:
    try:
        default_in = get_default_input_device_id()
    except OSError as exc:
        print(f"  ERROR reading default input: {exc}")
        default_in = None
    try:
        device_ids = list_audio_devices()
    except OSError as exc:
        print(f"  ERROR enumerating devices: {exc}")
        return

    rows = []
    for dev_id in device_ids:
        s = _device_summary(dev_id)
        if not include_output and not s["has_input"]:
            continue
        rows.append(s)

    if not rows:
        print("  (no audio input devices found)")
        return

    print(
        f"  default input device id: {default_in}\n"
        f"  {'id':>4}  {'in':>2}  {'out':>3}  "
        f"{'somewhere(in)':>13}  {'somewhere(g)':>12}  {'running':>7}  "
        f"{'hog':>5}  {'transport':>16}  name (uid)"
    )
    print("  " + "-" * 110)
    for s in rows:
        marker = " *" if s["id"] == default_in else "  "
        print(
            f"{marker}{s['id']:>4}  "
            f"{'Y' if s['has_input'] else '-':>2}  "
            f"{'Y' if s['has_output'] else '-':>3}  "
            f"{_bool_cell(s['running_somewhere_input']):>13}  "
            f"{_bool_cell(s['running_somewhere_global']):>12}  "
            f"{_bool_cell(s['running_input']):>7}  "
            f"{(s['hog_pid'] if s['hog_pid'] not in (None, -1) else '-'):>5}  "
            f"{s['transport']:>16}  "
            f"{s['name'] or '<no name>'} ({s['uid'] or '?'})"
        )
    print("\n  ('* ' marks the system default input)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true",
                    help="Re-snapshot every --interval seconds until Ctrl-C")
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--include-output", action="store_true",
                    help="Also include output-only devices in the table")
    args = ap.parse_args()

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        _print_one_pass(args.include_output)
        return

    print("Watching every input device. Try: join Zoom / Meet / Discord.")
    print("Look for a '**YES**' to appear in any 'somewhere' column.\n")
    try:
        while True:
            print("\n" + "=" * 110)
            print(f"[{time.strftime('%H:%M:%S')}]")
            _print_one_pass(args.include_output)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
