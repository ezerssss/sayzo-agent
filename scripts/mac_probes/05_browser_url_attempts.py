#!/usr/bin/env python3
"""Probe 5 — Three ways to read the active browser tab URL on macOS.

Today the agent does NOT read tab URLs on macOS — ``platform_mac.py``
explicitly returns [] for ``get_browser_window_urls`` to avoid the
Automation TCC dialog. This means user-added detector specs that ONLY
have ``url_patterns`` (the entire Settings → Web tab UX) cannot match
on Mac. This probe tries 3 approaches and reports which work without
prompts:

  Method A: AX walk for an address-bar text field.
  Method B: AX URL attribute on the browser web area.
  Method C: AppleScript "get URL of active tab" (the path we currently
            avoid — included so you can compare results when you accept
            the prompt).

You should see one or more of A/B succeed without a TCC prompt for some
browsers (Chrome / Edge expose AX text-field values pretty reliably).
Safari and Firefox vary.

Method C is the gold standard but triggers the "Sayzo wants to control
Google Chrome" dialog. We're including it so we know what a *correct*
answer looks like when the user accepts; if the AX methods agree, we
don't need C.

Dependencies:
    pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices

Usage:
    python3 05_browser_url_attempts.py
    python3 05_browser_url_attempts.py --browsers chrome safari --skip-applescript

Open a known URL (e.g. https://meet.google.com/abc-defg-hij) in each
browser before running, so you can verify the readouts.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

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
        kAXChildrenAttribute,
        kAXRoleAttribute,
        kAXValueAttribute,
        kAXTitleAttribute,
    )
except ImportError:
    print("ERROR: pip install pyobjc-framework-ApplicationServices")
    sys.exit(1)


BROWSER_BUNDLES = {
    "chrome": ("com.google.Chrome", "Google Chrome"),
    "safari": ("com.apple.Safari", "Safari"),
    "edge": ("com.microsoft.edgemac", "Microsoft Edge"),
    "brave": ("com.brave.Browser", "Brave Browser"),
    "arc": ("company.thebrowser.Browser", "Arc"),
    "firefox": ("org.mozilla.firefox", "Firefox"),
}


# ---- common AX helpers --------------------------------------------------

def _pids_for_bundle(bundle_id: str) -> list[int]:
    ws = NSWorkspace.sharedWorkspace()
    out = []
    for app in ws.runningApplications():
        try:
            if str(app.bundleIdentifier() or "") == bundle_id:
                pid = int(app.processIdentifier() or 0)
                if pid > 0:
                    out.append(pid)
        except Exception:
            continue
    return out


def _attr(elem, attr) -> object:
    try:
        err, value = AXUIElementCopyAttributeValue(elem, attr, None)
    except Exception:
        return None
    if err != 0:
        return None
    return value


def _walk(elem, depth: int = 0, max_depth: int = 6):
    """Yield (depth, role, value, title, elem) for elem and descendants
    breadth-first up to max_depth."""
    if depth > max_depth:
        return
    role = _attr(elem, kAXRoleAttribute)
    value = _attr(elem, kAXValueAttribute)
    title = _attr(elem, kAXTitleAttribute)
    yield depth, role, value, title, elem
    children = _attr(elem, kAXChildrenAttribute)
    if not children:
        return
    for c in children:
        yield from _walk(c, depth + 1, max_depth)


# ---- Method A: address-bar text field walk ------------------------------

def method_a_addressbar_walk(pid: int) -> Optional[str]:
    """Walk the AX tree, return the first AXTextField value that looks
    URL-ish."""
    app_ref = AXUIElementCreateApplication(pid)
    windows = _attr(app_ref, kAXWindowsAttribute)
    if not windows:
        return None
    for window in windows:
        for depth, role, value, _title, _elem in _walk(window):
            if role == "AXTextField" and isinstance(value, str):
                v = value.strip()
                if v.startswith(("http://", "https://", "about:", "chrome://",
                                 "edge://", "brave://", "arc://", "vivaldi://",
                                 "view-source:")):
                    return v
                # Chrome/Edge sometimes show the URL with the scheme
                # stripped; accept bare host-like strings too.
                if "." in v and " " not in v and v.lower() == v:
                    return v
    return None


# ---- Method B: AXWebArea + AXURL ---------------------------------------

def method_b_webarea_url(pid: int) -> Optional[str]:
    """Find an AXWebArea element and read its AXURL attribute.

    Many browsers expose the page URL on the AXWebArea for screen-reader
    consumption. The attribute name is the literal string ``"AXURL"``.
    """
    app_ref = AXUIElementCreateApplication(pid)
    windows = _attr(app_ref, kAXWindowsAttribute)
    if not windows:
        return None
    for window in windows:
        for depth, role, _value, _title, elem in _walk(window, max_depth=8):
            if role == "AXWebArea":
                url = _attr(elem, "AXURL")
                if url is None:
                    continue
                # AXURL returns either an NSURL or a string depending on browser.
                try:
                    s = str(url)
                except Exception:
                    continue
                if s:
                    return s
    return None


# ---- Method C: AppleScript ---------------------------------------------

_APPLESCRIPT = {
    "chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "edge":   'tell application "Microsoft Edge" to get URL of active tab of front window',
    "brave":  'tell application "Brave Browser" to get URL of active tab of front window',
    "arc":    'tell application "Arc" to get URL of active tab of front window',
    "safari": 'tell application "Safari" to get URL of front document',
    # Firefox doesn't ship a usable AppleScript dictionary; skipped.
}


def method_c_applescript(short_name: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (url, error). Will trigger Automation TCC dialog the first
    time per browser."""
    script = _APPLESCRIPT.get(short_name)
    if script is None:
        return None, "no AppleScript path for this browser"
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, "osascript timed out (likely waiting on TCC dialog)"
    except Exception as exc:
        return None, f"osascript subprocess error: {exc}"
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"exit code {proc.returncode}"
    out = proc.stdout.strip()
    return (out or None), None


# ---- driver -------------------------------------------------------------

def _probe_browser(short_name: str, skip_applescript: bool) -> None:
    bundle, display = BROWSER_BUNDLES[short_name]
    pids = _pids_for_bundle(bundle)
    print(f"\n=== {display}  ({bundle}) ===")
    if not pids:
        print(f"  not running")
        return
    print(f"  pids: {pids}")
    pid = pids[0]

    print(f"  Method A (AX text-field walk): ", end="", flush=True)
    try:
        a = method_a_addressbar_walk(pid)
    except Exception as exc:
        a = None
        print(f"ERROR {exc}")
    else:
        print(repr(a))

    print(f"  Method B (AX WebArea URL):     ", end="", flush=True)
    try:
        b = method_b_webarea_url(pid)
    except Exception as exc:
        b = None
        print(f"ERROR {exc}")
    else:
        print(repr(b))

    if skip_applescript:
        print(f"  Method C (AppleScript):        skipped")
        return
    print(f"  Method C (AppleScript):        ", end="", flush=True)
    c, err = method_c_applescript(short_name)
    if err:
        print(f"ERROR  {err}")
    else:
        print(repr(c))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--browsers", nargs="*",
                    default=list(BROWSER_BUNDLES.keys()),
                    choices=list(BROWSER_BUNDLES.keys()),
                    help="Which browsers to probe (default: all known)")
    ap.add_argument("--skip-applescript", action="store_true",
                    help="Skip Method C (the one that triggers TCC dialogs)")
    args = ap.parse_args()

    print("Probe 5 — browser tab URL reads.")
    print("Open a meeting URL (e.g. https://meet.google.com/<some-code>) in")
    print("each browser before running; verify the readouts match.")
    if not args.skip_applescript:
        print("\nNOTE: Method C will trigger 'Sayzo wants to control X' dialogs")
        print("(actually 'Python wants to control X' since this is a script).")
        print("Use --skip-applescript to suppress.")

    for name in args.browsers:
        _probe_browser(name, args.skip_applescript)


if __name__ == "__main__":
    main()
