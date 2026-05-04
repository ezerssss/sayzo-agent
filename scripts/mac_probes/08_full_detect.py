#!/usr/bin/env python3
"""Probe 8 — End-to-end meeting detection using the Swift helper + AX titles.

Closes the loop on the new architecture by composing every piece we
verified separately:

  1. Run ``./07_swift_audio_detect --json`` to get every audio process
     with ``is_running_input=YES``. Per-process attribution that probe 07
     proved works on this Mac.
  2. For each capturing process, walk parent PIDs (psutil) until we find
     either a known browser or a known meeting app. (Browser audio
     happens in helper processes like ``com.apple.webkit.GPU``; the
     parent or grandparent is the actual browser.)
  3. If the owner is a desktop meeting app → MATCH (Pass 1, Windows-equivalent).
  4. If the owner is a browser → read every AX window title for that
     browser → match against meeting URL/title patterns → MATCH.

NO foreground requirement at any step. Works for Safari, Chrome, Edge,
Brave, Arc, Firefox.

Prereqs (in this directory):
  - 07_swift_audio_detect compiled (the binary, not just the source)
  - psutil + pyobjc-framework-Cocoa + pyobjc-framework-ApplicationServices
  - Accessibility granted to whichever Python runs this script

Usage:
    python3 08_full_detect.py
    python3 08_full_detect.py --watch
    python3 08_full_detect.py --binary /path/to/07_swift_audio_detect
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except ImportError:
    print("ERROR: pip install psutil")
    sys.exit(1)

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
        kAXChildrenAttribute,
        kAXRoleAttribute,
        kAXValueAttribute,
    )
except ImportError:
    print("ERROR: pip install pyobjc-framework-ApplicationServices")
    sys.exit(1)


# ---- vendored matcher (same as probe 04, browser specs only) ------------

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
    Spec("zoom", "Zoom", bundle_ids=["us.zoom.xos"]),
    Spec("teams_desktop", "Microsoft Teams",
         bundle_ids=["com.microsoft.teams", "com.microsoft.teams2"]),
    Spec("discord", "Discord", bundle_ids=["com.hnc.Discord"]),
    Spec("slack", "Slack", bundle_ids=["com.tinyspeck.slackmacgap"]),
    # Web counterparts for the desktop meeting apps. Same display_name as
    # the desktop spec so the consent-toast copy doesn't surface the
    # implementation detail of "is this the desktop or web one".
    Spec("discord_web", "Discord", is_browser=True,
         url_patterns=[r"^https://discord\.com/channels/"],
         title_patterns=[r"(?i)\bDiscord\b"]),
    Spec("slack_web", "Slack", is_browser=True,
         url_patterns=[r"^https://app\.slack\.com/client/"],
         title_patterns=[r"(?i)\bSlack\b"]),
    Spec("skype_web", "Skype", is_browser=True,
         url_patterns=[r"^https://web\.skype\.com/"],
         title_patterns=[r"(?i)\bSkype\b"]),
    Spec("whatsapp_web", "WhatsApp", is_browser=True,
         url_patterns=[r"^https://web\.whatsapp\.com/"],
         title_patterns=[r"(?i)\bWhatsApp\b"]),
    Spec("webex", "Webex",
         bundle_ids=["Cisco-Systems.Spark", "com.webex.meetingmanager"]),
    Spec("skype", "Skype", bundle_ids=["com.skype.skype"]),
    Spec("facetime", "FaceTime", bundle_ids=["com.apple.FaceTime"]),
    Spec("whatsapp", "WhatsApp", bundle_ids=["net.whatsapp.WhatsApp"]),
    Spec("signal", "Signal",
         bundle_ids=["org.whispersystems.signal-desktop"]),
    Spec("gotomeeting", "GoTo", bundle_ids=["com.logmein.GoToMeeting"]),
    Spec("bluejeans", "BlueJeans", bundle_ids=["com.bluejeans.app"]),
    Spec("chime", "Amazon Chime", bundle_ids=["com.amazon.Chime"]),
    Spec("ringcentral", "RingCentral",
         bundle_ids=["com.ringcentral.rcoffice"]),
    Spec("dialpad", "Dialpad", bundle_ids=["co.dialpad.dialpad"]),

    Spec("gmeet", "Google Meet", is_browser=True,
         url_patterns=[
             r"^https://meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}",
         ],
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


# Browser bundle id → display name. Same set as platform_mac.py.
BROWSER_BUNDLES: dict[str, str] = {
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.brave.Browser": "Brave Browser",
    "company.thebrowser.Browser": "Arc",
    "org.mozilla.firefox": "Firefox",
    "com.operasoftware.Opera": "Opera",
    "com.vivaldi.Vivaldi": "Vivaldi",
}


# Browser audio happens in helper processes whose bundle id starts with a
# recognizable prefix. We can't rely on parent-PID walking because most
# browsers (Safari/WebKit definitely, Chrome/Chromium often) launch their
# helpers via launchd, so the parent chain is just (helper → launchd) and
# we lose the connection to the actual browser. Prefix matching is the
# fast, reliable identification path.
#
# Each entry: (helper_bundle_prefix_lowercase, owning_browser_bundle_id).
# Order matters — first match wins. We list more-specific prefixes first
# so e.g. ``com.google.Chrome.helper.alerts`` matches the Chrome entry
# before falling through.
BROWSER_HELPER_PREFIXES: list[tuple[str, str]] = [
    # Safari: WebKit framework helpers. Apple uses both 'webkit' (lower)
    # and 'WebKit' (mixed) in different macOS versions; lowercase compare.
    ("com.apple.webkit.", "com.apple.Safari"),
    # Chrome / Chromium-derived: helper.gpu, helper.alerts, helper.renderer, etc.
    ("com.google.chrome.helper", "com.google.Chrome"),
    ("com.microsoft.edgemac.helper", "com.microsoft.edgemac"),
    ("com.brave.browser.helper", "com.brave.Browser"),
    ("company.thebrowser.browser.helper", "company.thebrowser.Browser"),
    ("com.operasoftware.opera.helper", "com.operasoftware.Opera"),
    ("com.vivaldi.vivaldi.helper", "com.vivaldi.Vivaldi"),
    # Firefox: WebRTC audio runs in the plugin-container helper. The parent
    # process IS firefox so parent-walking would also work, but prefix
    # matching keeps the code path uniform.
    ("org.mozilla.firefox.plugin-container", "org.mozilla.firefox"),
]


def browser_for_helper_bundle(bundle: Optional[str]) -> Optional[str]:
    """If ``bundle`` is a known browser helper bundle id, return the
    owning browser's bundle id. Else None."""
    if not bundle:
        return None
    bl = bundle.lower()
    for prefix, browser in BROWSER_HELPER_PREFIXES:
        if bl.startswith(prefix):
            return browser
    return None


# ---- Swift helper invocation -------------------------------------------

def run_swift_helper(binary_path: str) -> list[dict]:
    proc = subprocess.run(
        [binary_path, "--json"], capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Swift helper exited {proc.returncode}\n  stderr: {proc.stderr}"
        )
    if proc.stderr.strip():
        # The Swift binary writes diagnostic warnings to stderr. Surface
        # them so we don't silently miss "ProcessObjectList failed".
        sys.stderr.write(proc.stderr)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Swift helper produced non-JSON output: {exc}\n"
            f"  stdout (first 500): {proc.stdout[:500]!r}"
        )


# ---- parent PID walk → owning app --------------------------------------

def _bundle_for_pid(pid: int) -> Optional[str]:
    """Return the running NSRunningApplication bundle id for ``pid``,
    or None if no GUI app owns it (helper processes typically don't
    appear here themselves; their parent does)."""
    ws = NSWorkspace.sharedWorkspace()
    for app in ws.runningApplications():
        try:
            if int(app.processIdentifier() or 0) == pid:
                bid = str(app.bundleIdentifier() or "") or None
                return bid
        except Exception:
            continue
    return None


def _running_pid_for_bundle(bundle: str) -> Optional[int]:
    ws = NSWorkspace.sharedWorkspace()
    for app in ws.runningApplications():
        try:
            if str(app.bundleIdentifier() or "") == bundle:
                pid = int(app.processIdentifier() or 0)
                if pid > 0:
                    return pid
        except Exception:
            continue
    return None


def _walk_to_gui_ancestor(
    start_pid: int, max_depth: int = 6,
) -> tuple[Optional[int], Optional[str]]:
    """Walk parent PIDs from ``start_pid`` until we hit a process that
    has an ``NSRunningApplication`` entry (a real user-facing GUI app).

    Returns ``(found_pid, bundle_id)`` or ``(None, None)`` if the walk
    falls off into launchd / kernel without finding a GUI ancestor.
    """
    cur = start_pid
    for _ in range(max_depth):
        bid = _bundle_for_pid(cur)
        if bid is not None:
            return cur, bid
        try:
            parent = psutil.Process(cur).parent()
        except Exception:
            return None, None
        if parent is None or parent.pid in (0, 1):
            return None, None
        cur = parent.pid
    return None, None


def find_owning_app(
    pid: int,
    capturing_bundle: Optional[str],
    responsible_pid: Optional[int],
    max_depth: int = 6,
) -> tuple[Optional[int], Optional[str], str]:
    """Identify the GUI app that owns the audio-capturing process.

    Returns ``(owning_pid, bundle_id, source)`` where ``source`` records
    which resolution path won — useful for telling responsibility-SPI
    hits apart from fallback heuristics.

    Resolution order, most authoritative first:

      1. Responsibility SPI (Swift-side) → walk-up to GUI ancestor.
         The SPI is the OS's own privacy-attribution source; when it
         points to a deep helper that has no NSRunningApplication entry
         (e.g. Discord's Renderer → Discord Helper), we keep walking up
         until we hit the user-facing app. This is the production path.
      2. Bundle-id prefix → known browser. Defensive fallback for the
         WebKit case where the SPI returns the helper itself and the
         parent chain is launchd-rooted (no link to Safari).
      3. Plain parent-walk from the capturing PID. Catches anything (1)
         and (2) didn't reach.
    """
    # Pass 1 — responsibility SPI, with walk-up to GUI ancestor.
    if responsible_pid and responsible_pid > 0:
        owner_pid, bid = _walk_to_gui_ancestor(responsible_pid, max_depth=max_depth)
        if bid is not None:
            source = (
                "responsibility-spi"
                if owner_pid == responsible_pid
                else "spi+parent-walk"
            )
            return owner_pid, bid, source

    # Pass 2 — bundle-id prefix → known browser (fallback).
    browser_bundle = browser_for_helper_bundle(capturing_bundle)
    if browser_bundle is not None:
        return (
            _running_pid_for_bundle(browser_bundle),
            browser_bundle,
            "browser-helper-prefix-fallback",
        )

    # Pass 3 — parent-walk from the capturing PID itself.
    owner_pid, bid = _walk_to_gui_ancestor(pid, max_depth=max_depth)
    if bid is not None:
        return owner_pid, bid, "parent-walk"

    return None, None, "no-owner"


# ---- AX titles for a browser bundle ------------------------------------

def _pids_for_bundle(bundle: str) -> list[int]:
    ws = NSWorkspace.sharedWorkspace()
    out = []
    for app in ws.runningApplications():
        try:
            if str(app.bundleIdentifier() or "") == bundle:
                pid = int(app.processIdentifier() or 0)
                if pid > 0:
                    out.append(pid)
        except Exception:
            continue
    return out


def ax_titles_for_bundle(bundle: str) -> list[str]:
    titles: list[str] = []
    for pid in _pids_for_bundle(bundle):
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
                titles.append(t)
    return titles


def _ax_attr(elem, attr):
    try:
        err, value = AXUIElementCopyAttributeValue(elem, attr, None)
    except Exception:
        return None
    return None if err != 0 else value


def _walk_ax(elem, depth: int = 0, max_depth: int = 8):
    yield depth, elem
    if depth >= max_depth:
        return
    children = _ax_attr(elem, kAXChildrenAttribute)
    if not children:
        return
    for c in children:
        yield from _walk_ax(c, depth + 1, max_depth)


def ax_urls_for_bundle(bundle: str) -> list[str]:
    """Read active-tab URLs from a browser via Accessibility.

    Two strategies, run per browser process:
      A) AXURL attribute on AXWebArea elements (semantically correct;
         what screen readers use). Works for Safari and a few others.
      B) AXValue on AXTextField elements that look URL-shaped (the
         omnibox text). Backup that catches Edge sometimes.

    Probe 05 confirmed Safari supports A and B; Chrome supports
    NEITHER without an Automation TCC prompt. So this returns useful
    URLs for Safari/Edge/Brave/Arc with mixed luck and an empty list
    for Chrome — the matcher then falls back to title patterns for
    Chrome users.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: object) -> None:
        if u is None:
            return
        try:
            s = str(u).strip()
        except Exception:
            return
        if not s or s in seen:
            return
        if not s.startswith(("http://", "https://", "file://")):
            return
        seen.add(s)
        urls.append(s)

    for pid in _pids_for_bundle(bundle):
        try:
            app_ref = AXUIElementCreateApplication(pid)
        except Exception:
            continue
        windows = _ax_attr(app_ref, kAXWindowsAttribute) or []
        for window in windows:
            for _depth, elem in _walk_ax(window, max_depth=8):
                role = _ax_attr(elem, kAXRoleAttribute)
                if role == "AXWebArea":
                    _add(_ax_attr(elem, "AXURL"))
                elif role == "AXTextField":
                    _add(_ax_attr(elem, kAXValueAttribute))
    return urls


# ---- matcher -----------------------------------------------------------

def match_browser_spec(urls: list[str], titles: list[str]) -> Optional[Spec]:
    """Match every browser DetectorSpec against a flat list of URLs and
    titles harvested from a browser's AX tree.

    URL patterns are tried against ``urls`` first (preferred — works for
    user-added custom detectors that only have URL patterns) and then
    against ``titles`` as a legacy fallback. Title patterns are tried
    against ``titles`` only. Same precedence as the agent's existing
    ``_browser_spec_matches`` in ``arm/detectors.py``.
    """
    for spec in SPECS:
        if not spec.is_browser:
            continue
        for pat in spec.url_patterns:
            rx = re.compile(pat)
            for u in urls:
                if rx.search(u):
                    return spec
            for t in titles:
                if rx.search(t):
                    return spec
        for pat in spec.title_patterns:
            rx = re.compile(pat)
            for t in titles:
                if rx.search(t):
                    return spec
    return None


def desktop_spec_for_bundle(bundle: str) -> Optional[Spec]:
    """Match a bundle id against the desktop DetectorSpecs.

    Handles helper-bundle ids by prefix (e.g. ``com.hnc.Discord.helper.Renderer``
    matches the ``com.hnc.Discord`` spec) so we don't depend solely on the
    SPI walk-up to find Discord when its capturing process is a Renderer
    helper. Belt-and-suspenders with ``find_owning_app``.
    """
    bl = bundle.lower()
    for spec in SPECS:
        if spec.is_browser:
            continue
        for spec_bundle in spec.bundle_ids:
            sb = spec_bundle.lower()
            if bl == sb or bl.startswith(sb + "."):
                return spec
    return None


# ---- driver -------------------------------------------------------------

def _print_one_pass(binary_path: str) -> None:
    try:
        procs = run_swift_helper(binary_path)
    except Exception as exc:
        print(f"  Swift helper invocation failed: {exc}")
        return

    capturing = [p for p in procs if p.get("input") == 1]
    print(f"  audio processes capturing input: {len(capturing)}")
    if not capturing:
        print("  → NO MATCH (nothing is using the mic)")
        return

    matched_apps: list[tuple[Spec, str]] = []
    for proc in capturing:
        pid = proc.get("pid", -1)
        bundle = proc.get("bundle_id") or ""
        responsible = proc.get("responsible_pid", -1)
        print(f"\n  capturing pid={pid} responsible_pid={responsible} bundle={bundle!r}")

        # Pass A: direct desktop match by capturing-process bundle.
        spec = desktop_spec_for_bundle(bundle)
        if spec is not None:
            print(f"    → desktop match: {spec.app_key} ({spec.display_name})")
            matched_apps.append((spec, "desktop direct"))
            continue

        # Pass B: walk to owning app. If owning is a known browser,
        # try title matching. If owning is a desktop meeting app
        # (some apps capture via a helper), match too.
        owner_pid, owner_bundle, source = find_owning_app(pid, bundle, responsible)
        print(f"    owning app: pid={owner_pid} bundle={owner_bundle!r}  [{source}]")
        if owner_bundle is None:
            print(f"    → no known owner — skipping")
            continue

        spec = desktop_spec_for_bundle(owner_bundle)
        if spec is not None:
            print(f"    → desktop match via owner: {spec.app_key}")
            matched_apps.append((spec, "desktop via owner"))
            continue

        if owner_bundle in BROWSER_BUNDLES:
            browser_name = BROWSER_BUNDLES[owner_bundle]
            titles = ax_titles_for_bundle(owner_bundle)
            urls = ax_urls_for_bundle(owner_bundle)
            print(f"    {browser_name} window titles: {titles}")
            print(f"    {browser_name} AX URLs:       {urls if urls else '(none — AX URL not exposed by this browser)'}")
            if not titles and not urls:
                print(f"    → browser is capturing but AX returned no titles OR urls")
                print(f"      (Accessibility permission may not be granted)")
                continue
            spec = match_browser_spec(urls, titles)
            if spec is not None:
                via = "url" if (
                    urls and any(re.search(p, u) for p in spec.url_patterns for u in urls)
                ) else "title"
                print(f"    → browser match: {spec.app_key} ({spec.display_name})")
                matched_apps.append((spec, f"browser via {browser_name} {via}"))
            else:
                print(f"    → browser is capturing but no url/title matched any spec")
        else:
            print(f"    → owner is neither a known browser nor a known meeting app")

    print()
    if matched_apps:
        for spec, source in matched_apps:
            print(f"  >>> WOULD ARM for: {spec.app_key} ({spec.display_name})")
            print(f"      via {source}")
    else:
        print("  >>> NO MATCH this pass — see per-process diagnostics above")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--binary", default="./07_swift_audio_detect",
                    help="Path to the compiled Swift helper")
    args = ap.parse_args()

    if not os.path.exists(args.binary):
        print(f"ERROR: Swift helper not found at {args.binary}")
        print("Compile it first:")
        print("  swiftc -O -o 07_swift_audio_detect 07_swift_audio_detect.swift \\")
        print("      -framework CoreAudio -framework Foundation")
        sys.exit(1)

    if not args.watch:
        print(f"[{time.strftime('%H:%M:%S')}]")
        _print_one_pass(args.binary)
        return

    print("Full meeting-detection flow (Swift audio + AX titles).")
    print("Try: join Zoom (no foreground), join Meet in Safari (no foreground),")
    print("Discord call, Teams web, etc. Ctrl-C to stop.")
    try:
        while True:
            print("\n" + "=" * 72)
            print(f"[{time.strftime('%H:%M:%S')}]")
            _print_one_pass(args.binary)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
