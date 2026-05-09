"""Reproduce the Settings show-during-init race in isolation.

Spawns the Settings subprocess in ``--idle`` mode and sends ``show`` after a
small delay (default 500 ms). That's roughly the timing the agent's
``_tray_bridge`` hits on a user-launch boot: ``settings_event`` is set
before the subprocess starts, the subprocess is spawned, then ~500 ms
later the bridge's first poll tells it to ``show`` — well before
pywebview / WebView2 finishes initial load.

Use this to test whether the "lags eventually after Quit + relaunch"
symptom reproduces without the full agent in the picture.

Usage::

    python scripts/repro_settings_race.py              # 500 ms — tight race
    python scripts/repro_settings_race.py --delay 100  # tighter still
    python scripts/repro_settings_race.py --delay 5000 # well past init (control)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=int,
        default=500,
        help="ms to wait after spawn before sending the show command",
    )
    args = parser.parse_args()

    argv = [sys.executable, "-m", "sayzo_agent", "settings", "--idle"]
    print(f"[repro] spawning: {argv}", flush=True)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE)

    print(f"[repro] sleeping {args.delay} ms before sending 'show'", flush=True)
    time.sleep(args.delay / 1000.0)

    print("[repro] sending: show", flush=True)
    assert proc.stdin is not None
    proc.stdin.write(b"show\n")
    proc.stdin.flush()

    print(f"[repro] subprocess pid={proc.pid} — Ctrl+C to quit", flush=True)
    try:
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.stdin.write(b"quit\n")
            proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
