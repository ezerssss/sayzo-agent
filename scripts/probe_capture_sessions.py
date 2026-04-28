"""Diagnose whether Chrome lingers in WASAPI State == Active after a tab
releases the mic.

Polls every 1 second and prints every WASAPI capture session it sees on
the default microphone, with each session's process name + PID + State.
Designed to answer the question "does the suppression-not-clearing bug
come from Chrome holding the WASAPI session in Active state, or
something else?"

How to read the output:
- State 1 (ACTIVE) — actively capturing audio.
- State 0 (Inactive) — session exists but isn't capturing right now.
- State 2 (Expired) — gone, will be cleaned up.

Sayzo's mic-holder query (sayzo_agent/arm/platform_win.py) only treats
State == 1 sessions as "holding the mic". So if Chrome shows State == 1
even after you've ended a voice mode, that's the bug — Chrome's session
lingers Active and Sayzo's decline-release timer never starts.

Run from the project venv:
    .venv\\Scripts\\activate
    python scripts/probe_capture_sessions.py

Suggested test: start it, open chatgpt.com, start voice mode, end voice
mode, watch the State column for chrome.exe. If chrome.exe stays at
State 1 for >5 seconds after you end voice mode, Hypothesis A is
confirmed.

Stops on Ctrl+C.
"""
from __future__ import annotations

import time
from typing import Optional

import pythoncom
import psutil

from comtypes import CLSCTX_ALL, CoCreateInstance
from pycaw.constants import CLSID_MMDeviceEnumerator
from pycaw.pycaw import (
    IAudioSessionControl2,
    IAudioSessionManager2,
    IMMDeviceEnumerator,
)


EDATAFLOW_CAPTURE = 1
EROLE_CONSOLE = 0

STATE_LABELS = {0: "Inactive", 1: "ACTIVE  ", 2: "Expired "}


def snapshot_capture_sessions() -> list[tuple[str, int, int]]:
    """Return ``(process_name, pid, state)`` for every capture session on
    the default microphone. Empty list on enumeration failure."""
    try:
        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            IMMDeviceEnumerator,
            CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(
            EDATAFLOW_CAPTURE, EROLE_CONSOLE
        )
        raw = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
        mgr = raw.QueryInterface(IAudioSessionManager2)
        session_enum = mgr.GetSessionEnumerator()
        count = session_enum.GetCount()
    except Exception as exc:
        print(f"[!] enumerator failed: {exc}")
        return []

    out: list[tuple[str, int, int]] = []
    for i in range(count):
        try:
            ctrl = session_enum.GetSession(i)
            ctrl2 = ctrl.QueryInterface(IAudioSessionControl2)
            state = ctrl.GetState()
            pid = ctrl2.GetProcessId()
        except Exception:
            out.append(("(inspect failed)", -1, -1))
            continue
        name = _process_name(pid)
        out.append((name, pid, state))
    return out


def _process_name(pid: int) -> str:
    if pid <= 0:
        return "(system audio)"
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "(dead pid)"


def _format_row(prefix: str, name: str, pid: int, state: int) -> str:
    label = STATE_LABELS.get(state, f"unknown({state})")
    return f"{prefix:<10}  {name:<26}  {pid:>7}  {state}  {label}"


def main() -> None:
    pythoncom.CoInitialize()
    print("Probing default-mic capture sessions every 1s. Ctrl+C to stop.")
    print()
    print(f"{'time':<10}  {'process':<26}  {'pid':>7}  state")
    print("-" * 64)

    last_signature: Optional[str] = None
    try:
        while True:
            ts = time.strftime("%H:%M:%S")
            sessions = snapshot_capture_sessions()
            # Build a stable signature so we can mark unchanged ticks
            # quietly — easier to spot the moment a State flips.
            sig = "|".join(
                f"{name}:{pid}:{state}" for name, pid, state in sessions
            )
            changed = sig != last_signature
            last_signature = sig

            marker = "  *" if changed else "   "
            if not sessions:
                print(f"{ts:<10}  (no capture sessions){marker}")
            else:
                for i, (name, pid, state) in enumerate(sessions):
                    prefix = ts if i == 0 else ""
                    suffix = marker if i == 0 else ""
                    print(_format_row(prefix, name, pid, state) + suffix)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
