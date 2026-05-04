#!/usr/bin/env python3
"""Probe 4 — End-to-end match simulation.

Runs every signal we have (CoreAudio mic-active, per-process audio
attribution, NSWorkspace foreground, AX titles) against the SHIPPED
DetectorSpec list and prints exactly what the agent's matcher would have
done — and *why*, when no match fires.

Two match passes are simulated side by side so we can compare:

  current  — exactly what arm/detectors.py::match_whitelist does today:
             - desktop apps need mic.active AND running AND foreground
             - browsers need browser to be foreground

  proposed — what we'd do with per-process attribution from probe 02:
             - desktop apps need their AudioProcessObject to have
               is_running_input=YES (no foreground requirement)
             - browsers need a browser AudioProcessObject with
               is_running_input=YES, then match URL/title

If "current" returns None but "proposed" returns a match, that's a clear
signal the rewrite is the right move.

Dependencies (same as probes 02 + 03):
    pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices

Usage:
    python3 04_match_simulation.py
    python3 04_match_simulation.py --watch
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from _common import (
    is_default_input_running_somewhere,
    snapshot_audio_processes,
)

try:
    from AppKit import NSWorkspace
except ImportError:
    print("ERROR: pip install pyobjc-framework-Cocoa")
    sys.exit(1)

try:
    from ApplicationServices import (  # type: ignore[import-not-found]
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        kAXWindowsAttribute,
        kAXTitleAttribute,
    )
except ImportError:
    print("ERROR: pip install pyobjc-framework-ApplicationServices")
    sys.exit(1)


# ---- vendored DetectorSpec list (mirrors config.default_detector_specs) -

@dataclass
class Spec:
    app_key: str
    display_name: str
    process_names: list[str] = field(default_factory=list)
    bundle_ids: list[str] = field(default_factory=list)
    is_browser: bool = False
    url_patterns: list[str] = field(default_factory=list)
    title_patterns: list[str] = field(default_factory=list)


SPECS: list[Spec] = [
    Spec("zoom", "Zoom", ["zoom.exe", "CptHost.exe"], ["us.zoom.xos"]),
    Spec("teams_desktop", "Microsoft Teams",
         ["ms-teams.exe", "Teams.exe"],
         ["com.microsoft.teams", "com.microsoft.teams2"]),
    Spec("discord", "Discord", ["Discord.exe"], ["com.hnc.Discord"]),
    Spec("slack", "Slack", ["slack.exe"], ["com.tinyspeck.slackmacgap"]),
    Spec("webex", "Webex",
         ["webex.exe", "CiscoCollabHost.exe"],
         ["Cisco-Systems.Spark", "com.webex.meetingmanager"]),
    Spec("skype", "Skype", ["Skype.exe"], ["com.skype.skype"]),
    Spec("facetime", "FaceTime", [], ["com.apple.FaceTime"]),
    Spec("whatsapp", "WhatsApp", ["WhatsApp.exe"], ["net.whatsapp.WhatsApp"]),
    Spec("signal", "Signal", ["Signal.exe"],
         ["org.whispersystems.signal-desktop"]),
    Spec("gotomeeting", "GoTo",
         ["g2mcomm.exe", "g2mlauncher.exe"],
         ["com.logmein.GoToMeeting"]),
    Spec("bluejeans", "BlueJeans", ["BlueJeans.exe"], ["com.bluejeans.app"]),
    Spec("chime", "Amazon Chime", ["Chime.exe"], ["com.amazon.Chime"]),
    Spec("ringcentral", "RingCentral",
         ["RingCentral.exe", "RCMeetings.exe"], ["com.ringcentral.rcoffice"]),
    Spec("dialpad", "Dialpad", ["Dialpad.exe"], ["co.dialpad.dialpad"]),

    Spec("gmeet", "Google Meet", is_browser=True,
         url_patterns=[r"^https://meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}"],
         title_patterns=[
             r"(?i)\bGoogle Meet\b", r"(?i)\bgmeet\b",
             r"\bMeet - [a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}\b",
         ]),
    Spec("teams_web", "Microsoft Teams", is_browser=True,
         url_patterns=[
             r"teams\.microsoft\.com/.+/l/meetup-join/",
             r"teams\.microsoft\.com/_#/conversations/.+/meeting",
         ],
         title_patterns=[r"(?i)\bMicrosoft Teams\b"]),
    Spec("zoom_web", "Zoom", is_browser=True,
         url_patterns=[r"^https://[^/]+\.zoom\.us/wc/join/",
                       r"^https://[^/]+\.zoom\.us/j/\d+"],
         title_patterns=[r"(?i)\bZoom Meeting\b"]),
    Spec("webex_web", "Webex", is_browser=True,
         url_patterns=[r"^https://[^/]+\.webex\.com/(meet|wbxmjs|webappng)/"],
         title_patterns=[r"(?i)\bwebex\b"]),
    Spec("whereby", "Whereby", is_browser=True,
         url_patterns=[r"^https://whereby\.com/[^/]+"],
         title_patterns=[r"(?i)\bwhereby\b"]),
    Spec("jitsi", "Jitsi Meet", is_browser=True,
         url_patterns=[r"^https://meet\.jit\.si/[^/]+"],
         title_patterns=[r"(?i)\bjitsi\b"]),
    Spec("8x8", "8x8 Meet", is_browser=True,
         url_patterns=[r"^https://8x8\.vc/[^/]+"],
         title_patterns=[r"(?i)\b8x8\b"]),
]


KNOWN_BROWSERS = {
    "com.google.Chrome", "com.apple.Safari", "com.microsoft.edgemac",
    "com.brave.Browser", "company.thebrowser.Browser", "org.mozilla.firefox",
    "com.operasoftware.Opera", "com.vivaldi.Vivaldi",
}


# ---- snapshot helpers ---------------------------------------------------

def _snapshot_foreground() -> dict:
    ws = NSWorkspace.sharedWorkspace()
    front = ws.frontmostApplication()
    if front is None:
        return {"bundle_id": None, "name": None, "pid": None,
                "is_browser": False}
    bid = str(front.bundleIdentifier() or "") or None
    return {
        "bundle_id": bid,
        "name": str(front.localizedName() or "") or None,
        "pid": int(front.processIdentifier() or 0) or None,
        "is_browser": bid in KNOWN_BROWSERS if bid else False,
    }


def _snapshot_running_bundles() -> dict[str, list[int]]:
    ws = NSWorkspace.sharedWorkspace()
    out: dict[str, list[int]] = {}
    for app in ws.runningApplications():
        try:
            bid = str(app.bundleIdentifier() or "") or None
            pid = int(app.processIdentifier() or 0) or None
        except Exception:
            continue
        if bid and pid:
            out.setdefault(bid, []).append(pid)
    return out


def _ax_titles_for_pids(pids: list[int]) -> list[str]:
    out: list[str] = []
    for pid in pids:
        try:
            app_ref = AXUIElementCreateApplication(pid)
        except Exception:
            continue
        try:
            err, windows = AXUIElementCopyAttributeValue(
                app_ref, kAXWindowsAttribute, None
            )
        except Exception:
            continue
        if err != 0 or not windows:
            continue
        for window in windows:
            try:
                err, title = AXUIElementCopyAttributeValue(
                    window, kAXTitleAttribute, None
                )
            except Exception:
                continue
            if err != 0 or not title:
                continue
            t = str(title).strip()
            if t:
                out.append(t)
    return out


# ---- matchers -----------------------------------------------------------

def _browser_spec_hits_titles(spec: Spec, titles: list[str]) -> bool:
    """Match URL patterns and title patterns against a list of titles."""
    for pat in spec.url_patterns:
        rx = re.compile(pat)
        for t in titles:
            if rx.search(t):
                return True
    for pat in spec.title_patterns:
        rx = re.compile(pat)
        for t in titles:
            if rx.search(t):
                return True
    return False


def match_current(
    fg: dict,
    mic_active: bool,
    running_bundles: dict[str, list[int]],
    browser_titles_per_bundle: dict[str, list[str]],
) -> tuple[Optional[Spec], str]:
    """What sayzo_agent/arm/detectors.py::match_whitelist would do TODAY."""
    fg_bundle = (fg["bundle_id"] or "").lower()
    fg_name = (fg["name"] or "").lower()

    # Pass 2 — mac desktop proxy (desktop apps).
    if mic_active and running_bundles:
        for spec in SPECS:
            if spec.is_browser:
                continue
            targets = {b.lower() for b in spec.bundle_ids} | {
                p.lower() for p in spec.process_names
            }
            if not targets:
                continue
            running_lower = {b.lower() for b in running_bundles}
            if not any(t in running_lower or t == fg_bundle or t == fg_name
                       for t in targets):
                continue
            if (fg_bundle and fg_bundle in targets) or (
                fg_name and fg_name in targets
            ):
                return spec, "current matched (mic_active + running + foreground)"

    # Pass 3 — browser. Requires browser foreground on macOS today.
    if mic_active and fg["is_browser"]:
        # Aggregate ALL browser titles (matches the agent's existing collect logic).
        all_titles: list[str] = []
        for titles in browser_titles_per_bundle.values():
            all_titles.extend(titles)
        for spec in SPECS:
            if not spec.is_browser:
                continue
            if _browser_spec_hits_titles(spec, all_titles):
                return spec, "current matched (browser foreground + title)"

    if not mic_active:
        return None, "current: mic not active"
    if not running_bundles:
        return None, "current: no running apps captured"
    if fg["is_browser"]:
        return None, ("current: browser foreground but no title pattern hit. "
                      "Either the meeting tab isn't open or its title doesn't "
                      "match any title_patterns regex.")
    return None, ("current: foreground is not a browser AND no whitelisted "
                  "desktop app is foreground. Today's matcher gives up here.")


def match_proposed(
    audio_procs: list[dict],
    browser_titles_per_bundle: dict[str, list[str]],
) -> tuple[Optional[Spec], str]:
    """What we'd do with per-process attribution from probe 02.

    Pass A: any AudioProcessObject with is_running_input=YES whose
            bundle_id matches a non-browser DetectorSpec → match.
    Pass B: any AudioProcessObject with is_running_input=YES whose
            bundle_id is a known browser → match the FIRST browser spec
            whose URL/title patterns hit any title from any browser.
    """
    capturing = [p for p in audio_procs if p.get("is_running_input") == 1]
    if not capturing:
        return None, "proposed: no AudioProcessObject has is_running_input=YES"

    # Pass A — desktop spec match by bundle id.
    for proc in capturing:
        bid = (proc.get("bundle_id") or "").lower()
        if not bid:
            continue
        if bid in {b.lower() for b in KNOWN_BROWSERS}:
            continue  # handle browsers in Pass B
        for spec in SPECS:
            if spec.is_browser:
                continue
            if bid in {b.lower() for b in spec.bundle_ids}:
                return spec, (f"proposed matched (desktop): "
                              f"bundle_id={bid} is_running_input=YES")

    # Pass B — any browser actually capturing → match by title pattern.
    capturing_browser_bundles = {
        (p.get("bundle_id") or "")
        for p in capturing
        if (p.get("bundle_id") or "") in KNOWN_BROWSERS
    }
    if capturing_browser_bundles:
        all_titles: list[str] = []
        for bundle in capturing_browser_bundles:
            all_titles.extend(browser_titles_per_bundle.get(bundle, []))
        for spec in SPECS:
            if not spec.is_browser:
                continue
            if _browser_spec_hits_titles(spec, all_titles):
                return spec, (f"proposed matched (browser): is_running_input=YES "
                              f"on {capturing_browser_bundles}")

        return None, (f"proposed: browser is capturing ({capturing_browser_bundles}) "
                      f"but no title_patterns matched. AX titles: {all_titles!r}")

    return None, ("proposed: process(es) capturing input but none match a "
                  "DetectorSpec bundle id. Capturing bundle_ids: "
                  + ", ".join(repr(p.get("bundle_id")) for p in capturing))


# ---- driver -------------------------------------------------------------

def _print_one_pass() -> None:
    fg = _snapshot_foreground()
    running_bundles = _snapshot_running_bundles()
    try:
        mic_active = is_default_input_running_somewhere()
    except OSError as exc:
        print(f"  ERROR reading mic-active: {exc}")
        return
    try:
        audio_procs = snapshot_audio_processes()
    except OSError as exc:
        print(f"  ERROR enumerating audio processes: {exc}")
        audio_procs = []

    # AX titles only for browsers that are running (skip Safari etc. if not running).
    browser_titles: dict[str, list[str]] = {}
    for bundle in KNOWN_BROWSERS:
        pids = running_bundles.get(bundle, [])
        if pids:
            browser_titles[bundle] = _ax_titles_for_pids(pids)

    print(f"\n  mic_active = {mic_active}")
    print(f"  foreground = {fg['bundle_id']} (is_browser={fg['is_browser']})")
    capturing = [
        f"{p.get('bundle_id') or '<no bundle>'} (pid={p.get('pid')})"
        for p in audio_procs
        if p.get("is_running_input") == 1
    ]
    print(f"  capturing input ({len(capturing)}): {capturing or '—'}")
    if browser_titles:
        print("  browser titles:")
        for bundle, titles in browser_titles.items():
            print(f"    {bundle}: {titles}")

    print()
    cur, cur_why = match_current(fg, mic_active, running_bundles, browser_titles)
    print(f"  CURRENT  → {cur.app_key if cur else 'NO MATCH'}    [{cur_why}]")

    prop, prop_why = match_proposed(audio_procs, browser_titles)
    print(f"  PROPOSED → {prop.app_key if prop else 'NO MATCH'}    [{prop_why}]")

    if (prop is not None) and (cur is None):
        print("\n  >>> proposed approach would have fired a consent toast here, "
              "current does NOT.")
    if (cur is not None) and (prop is None):
        print("\n  >>> current would fire but proposed wouldn't — investigate.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        _print_one_pass()
        return

    print("Simulating both matchers. Try: join Zoom, focus elsewhere; join Meet,")
    print("alt+tab to Slack; etc. Ctrl-C to stop.")
    try:
        while True:
            print("\n" + "=" * 72)
            print(f"[{time.strftime('%H:%M:%S')}]")
            _print_one_pass()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
