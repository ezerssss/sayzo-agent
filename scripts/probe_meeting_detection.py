"""Live probe of the meeting-detection pipeline. No capture, no agent.

Usage:
    python scripts/probe_meeting_detection.py          # one-shot
    python scripts/probe_meeting_detection.py --watch  # 2 s poll, Ctrl-C to stop

Run this while you're in (or about to join) a meeting. It prints exactly
what the whitelist watcher would see on this poll:

  1. All processes currently holding a capture session on the default mic
     (Windows via pycaw WASAPI session enumeration; macOS via CoreAudio
     ``kAudioDevicePropertyDeviceIsRunningSomewhere``).
  2. The frontmost app's process/bundle + window title + (macOS only) the
     active browser tab URL.
  3. Every visible browser window title (Windows only — macOS stubs this).
  4. The match result: which DetectorSpec fires, or ``None`` with a short
     explanation of why.

If the live agent isn't popping a consent toast when you expect one, this
script tells you which stage broke without having to read through the
heartbeat log.
"""
from __future__ import annotations

import argparse
import sys
import time

# Force UTF-8 stdout so window titles with unicode don't crash on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sayzo_agent.arm import detectors as _d
from sayzo_agent.config import default_detector_specs


def _get_queries():
    """Pick the real platform queries for this OS, same as ArmController does."""
    if sys.platform == "win32":
        from sayzo_agent.arm.platform_win import (
            get_browser_window_titles,
            get_foreground_info,
            get_mic_holders,
        )
        return (
            get_mic_holders,
            lambda: False,  # Win infers mic-active from holders; no separate query
            lambda: frozenset(),  # Win doesn't need running_processes
            get_foreground_info,
            get_browser_window_titles,
        )
    if sys.platform == "darwin":
        from sayzo_agent.arm.platform_mac import (
            get_browser_window_titles,
            get_foreground_info,
            get_mic_holders,
            get_running_processes,
            is_mic_active,
        )
        return (
            get_mic_holders,
            is_mic_active,
            get_running_processes,
            get_foreground_info,
            get_browser_window_titles,
        )
    raise SystemExit(f"Unsupported platform: {sys.platform}")


def _probe_once(specs, queries) -> None:
    (q_holders, q_active, q_running, q_fg, q_titles) = queries

    holders = q_holders() or []
    active = bool(q_active())
    running = q_running() or frozenset()
    fg = q_fg()
    titles = q_titles() or []

    mic = _d.MicState(
        holders=list(holders),
        active=active,
        running_processes=running,
    )
    if titles:
        from dataclasses import replace
        fg = replace(fg, browser_window_titles=tuple(titles))

    print("=" * 72)
    print(f"{time.strftime('%H:%M:%S')}  platform={sys.platform}")

    print("\n[mic holders]")
    if not holders:
        print("  (none — Windows: no WASAPI capture session open on default mic;")
        print("         macOS: always empty, see `mic active` below)")
    for h in holders:
        print(f"  - {h.process_name} (pid={h.pid})")
    if sys.platform == "darwin":
        print(f"  mic active (system-wide): {active}")
    print(f"  running processes visible to matcher: {len(running)}")

    print("\n[foreground]")
    print(f"  process_name      : {fg.process_name}")
    print(f"  bundle_id         : {fg.bundle_id}")
    print(f"  window_title      : {fg.window_title!r}")
    print(f"  is_browser        : {fg.is_browser}")
    print(f"  browser_tab_url   : {fg.browser_tab_url!r}")
    print(f"  browser_tab_title : {fg.browser_tab_title!r}")

    print("\n[browser window titles]")
    if not fg.browser_window_titles:
        print("  (none)")
    for t in fg.browser_window_titles:
        print(f"  - {t!r}")

    print("\n[match result]")
    result = _d.match_whitelist(specs, fg, mic)
    if result is None:
        print("  None")
        print("  why:")
        if not holders and sys.platform == "win32":
            print("    - Windows has no capture sessions on the default mic.")
            print("      Check that your meeting app is actually open and on the default device.")
        elif sys.platform == "darwin" and not active:
            print("    - macOS CoreAudio says no process is capturing the default input.")
        else:
            # We got signal but nothing matched. Spell out why per spec.
            print("    - Signals present but no DetectorSpec accepted them.")
            print("      Candidate reasons per spec:")
            for spec in specs:
                if not spec.is_browser:
                    continue
                if not _d._browser_holds_mic(mic, fg):
                    continue  # non-browser specs filter out at Pass 3 gate
                url = fg.browser_tab_url or ""
                candidate_titles = _d._collect_browser_titles(fg)
                if _d._browser_spec_matches(spec, url, candidate_titles):
                    continue
                print(f"      · {spec.app_key:12s}: URL or title patterns didn't match")
    else:
        print(f"  app_key     : {result.app_key}")
        print(f"  display_name: {result.display_name}")
        print(f"  source      : {result.source}")
        print("  → the live agent would fire a consent toast here.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--watch", action="store_true",
        help="Poll every 2 s until Ctrl-C instead of one-shot.",
    )
    ap.add_argument(
        "--interval", type=float, default=2.0,
        help="Watch poll interval in seconds (default 2).",
    )
    args = ap.parse_args()

    specs = default_detector_specs()
    queries = _get_queries()

    if not args.watch:
        _probe_once(specs, queries)
        return

    try:
        while True:
            _probe_once(specs, queries)
            print()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[probe] stopped")


if __name__ == "__main__":
    main()
