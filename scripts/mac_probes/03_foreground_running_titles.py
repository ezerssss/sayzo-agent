#!/usr/bin/env python3
"""Probe 3 — Foreground app + running apps + browser window titles.

Tests the OTHER half of the current detection pipeline (the half that's
NOT the mic check). Verifies:

  1. NSWorkspace.frontmostApplication() returns the bundle id we expect
     and matches the bundle_ids in our default DetectorSpecs.
  2. NSWorkspace.runningApplications() includes meeting apps with their
     real bundle ids (so we can spot typos like a stale Discord bundle).
  3. AXUIElementCopyAttributeValue(kAXWindowsAttribute, kAXTitleAttribute)
     returns titles for every browser window — the only signal we have
     for matching Meet / Zoom-web / Teams-web in the absence of URL reads.

Required permission: **Accessibility** (System Settings → Privacy &
Security → Accessibility) for the AX title walk. The agent already
requires this for the global hotkey, so if you've set the agent up you're
already granted. If AX titles all come back empty here, that's the
signal you need to grant Terminal.app (or whichever Python is running) AX.

Dependencies:
    pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices

Usage:
    python3 03_foreground_running_titles.py
    python3 03_foreground_running_titles.py --watch
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    from AppKit import NSWorkspace
except ImportError:
    print("ERROR: pyobjc-framework-Cocoa not installed.")
    print("       pip install pyobjc-framework-Cocoa")
    sys.exit(1)

try:
    from ApplicationServices import (  # type: ignore[import-not-found]
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        kAXWindowsAttribute,
        kAXTitleAttribute,
    )
    AX_AVAILABLE = True
except ImportError:
    print("WARN: pyobjc-framework-ApplicationServices not installed; AX titles")
    print("      will be skipped. pip install pyobjc-framework-ApplicationServices")
    AX_AVAILABLE = False


# Bundle ids of every browser the default DetectorSpecs care about. Match
# the keys in sayzo_agent/arm/platform_mac.py::_BROWSER_BUNDLES.
KNOWN_BROWSERS = {
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.brave.Browser": "Brave Browser",
    "company.thebrowser.Browser": "Arc",
    "org.mozilla.firefox": "Firefox",
    "com.operasoftware.Opera": "Opera",
    "com.vivaldi.Vivaldi": "Vivaldi",
}

# Bundle ids in the SHIPPED default whitelist. We highlight matches so
# you can immediately see which spec a running app would hit.
EXPECTED_BUNDLES = {
    "us.zoom.xos": "zoom",
    "com.microsoft.teams": "teams_desktop",
    "com.microsoft.teams2": "teams_desktop",
    "com.hnc.Discord": "discord",
    "com.tinyspeck.slackmacgap": "slack",
    "Cisco-Systems.Spark": "webex",
    "com.webex.meetingmanager": "webex",
    "com.skype.skype": "skype",
    "com.apple.FaceTime": "facetime",
    "net.whatsapp.WhatsApp": "whatsapp",
    "org.whispersystems.signal-desktop": "signal",
    "com.logmein.GoToMeeting": "gotomeeting",
    "com.bluejeans.app": "bluejeans",
    "com.amazon.Chime": "chime",
    "com.ringcentral.rcoffice": "ringcentral",
    "co.dialpad.dialpad": "dialpad",
}


def _frontmost() -> dict:
    ws = NSWorkspace.sharedWorkspace()
    front = ws.frontmostApplication()
    if front is None:
        return {"bundle_id": None, "name": None, "pid": None}
    return {
        "bundle_id": str(front.bundleIdentifier() or "") or None,
        "name": str(front.localizedName() or "") or None,
        "pid": int(front.processIdentifier() or 0) or None,
    }


def _running_apps() -> list[dict]:
    ws = NSWorkspace.sharedWorkspace()
    out = []
    for app in ws.runningApplications():
        try:
            bid = str(app.bundleIdentifier() or "") or None
            name = str(app.localizedName() or "") or None
            pid = int(app.processIdentifier() or 0) or None
        except Exception:
            continue
        out.append({"bundle_id": bid, "name": name, "pid": pid})
    return out


def _browser_window_titles(bundle_to_pids: dict[str, list[int]]) -> dict[str, list[str]]:
    """Walk AX trees for every running browser, return titles per bundle."""
    if not AX_AVAILABLE:
        return {}
    out: dict[str, list[str]] = {}
    for bundle in KNOWN_BROWSERS:
        pids = bundle_to_pids.get(bundle, [])
        if not pids:
            continue
        titles: list[str] = []
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
                title_str = str(title).strip()
                if title_str:
                    titles.append(title_str)
        if titles:
            out[bundle] = titles
    return out


def _print_snapshot() -> None:
    fg = _frontmost()
    apps = _running_apps()

    # bundle_id → [pid] (one bundle can have multiple PIDs for profile-isolated browsers)
    bundle_to_pids: dict[str, list[int]] = {}
    for a in apps:
        if a["bundle_id"] and a["pid"]:
            bundle_to_pids.setdefault(a["bundle_id"], []).append(a["pid"])

    print("\n[ frontmost ]")
    fg_bundle = fg["bundle_id"] or "<none>"
    matched = EXPECTED_BUNDLES.get(fg_bundle)
    matched_browser = KNOWN_BROWSERS.get(fg_bundle)
    flag = ""
    if matched:
        flag = f"  ← matches DetectorSpec.app_key={matched!r}"
    elif matched_browser:
        flag = f"  ← is browser ({matched_browser})"
    print(f"  bundle_id : {fg_bundle}{flag}")
    print(f"  name      : {fg['name']}")
    print(f"  pid       : {fg['pid']}")

    print("\n[ running meeting apps from default whitelist ]")
    any_match = False
    for bundle, app_key in sorted(EXPECTED_BUNDLES.items(), key=lambda x: x[1]):
        if bundle in bundle_to_pids:
            any_match = True
            pids = bundle_to_pids[bundle]
            name = next(
                (a["name"] for a in apps if a["bundle_id"] == bundle), None
            )
            print(f"  {app_key:14s}  {bundle:42s}  pids={pids}  name={name!r}")
    if not any_match:
        print("  (none of the shipped meeting apps are running)")

    print("\n[ running browsers ]")
    any_browser = False
    for bundle, name in sorted(KNOWN_BROWSERS.items()):
        if bundle in bundle_to_pids:
            any_browser = True
            pids = bundle_to_pids[bundle]
            print(f"  {name:18s}  {bundle:32s}  pids={pids}")
    if not any_browser:
        print("  (no known browsers running)")

    if AX_AVAILABLE:
        titles = _browser_window_titles(bundle_to_pids)
        print("\n[ AX window titles per browser ]")
        if not titles:
            if not any_browser:
                print("  (no browsers running)")
            else:
                print("  (browsers running but AX returned NO titles —")
                print("   most likely Accessibility permission is not granted")
                print("   for the Python interpreter / Terminal running this script.)")
        for bundle, t_list in titles.items():
            name = KNOWN_BROWSERS.get(bundle, bundle)
            print(f"  {name}:")
            for t in t_list:
                print(f"    - {t!r}")
    else:
        print("\n[ AX titles skipped — pyobjc-framework-ApplicationServices missing ]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true",
                    help="Re-snapshot every --interval seconds until Ctrl-C")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        _print_snapshot()
        return

    print("Watching foreground / running apps / AX titles. Ctrl-C to stop.")
    print("Try: focus different apps, switch browser tabs, open Meet/Zoom-web.")
    try:
        while True:
            print("\n" + "=" * 72)
            print(f"[{time.strftime('%H:%M:%S')}]")
            _print_snapshot()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
