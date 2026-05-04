#!/usr/bin/env python3
"""Probe 1 — Does CoreAudio's "is the default input running" bit work?

This is the foundation of the whitelist watcher on macOS:
``arm/platform_mac.py::is_mic_active()`` calls
``kAudioDevicePropertyDeviceIsRunningSomewhere`` on the default input
device. If that bit is wrong (always False, always True, doesn't flip),
NOTHING downstream can work.

What you should see:
- IDLE: ``mic_active=False``.
- Open Zoom / Discord / Meet and join a call → ``mic_active=True`` within
  ~1 s.
- Mute yourself in the call → still ``True`` (muted users are still
  capturing — the mic stream is open, audio just isn't transmitted).
- Leave the call → back to ``False`` within ~1 s.

If you ever see the bit STAY False during a real call, that tells us the
whole macOS detection design needs a different foundation (e.g. the new
per-process API in script 02).

Usage:
    python3 01_mic_active.py            # one-shot
    python3 01_mic_active.py --watch    # poll every 1 s; Ctrl-C to stop
"""
from __future__ import annotations

import argparse
import sys
import time

from _common import get_default_input_device_id, is_default_input_running_somewhere


def probe_once() -> None:
    try:
        dev_id = get_default_input_device_id()
    except OSError as exc:
        print(f"  ERROR reading default input device: {exc}")
        return
    if dev_id is None:
        print("  default input device: <none> — system has no default mic")
        return

    try:
        active = is_default_input_running_somewhere()
    except OSError as exc:
        print(f"  ERROR reading IsRunningSomewhere: {exc}")
        return

    print(
        f"  default input AudioObjectID = {dev_id}    "
        f"mic_active = {active}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true",
                    help="Poll every --interval seconds until Ctrl-C")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        probe_once()
        return

    print("Watching default input device. Ctrl-C to stop.")
    print("Try: join a call, mute, unmute, leave.")
    last_state: bool | None = None
    try:
        while True:
            try:
                active = is_default_input_running_somewhere()
            except OSError as exc:
                print(f"[{time.strftime('%H:%M:%S')}] ERROR: {exc}")
                time.sleep(args.interval)
                continue
            tag = ""
            if last_state is None:
                tag = " (initial)"
            elif active != last_state:
                tag = "  <<< STATE CHANGED"
            last_state = active
            print(f"[{time.strftime('%H:%M:%S')}] mic_active = {active}{tag}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
