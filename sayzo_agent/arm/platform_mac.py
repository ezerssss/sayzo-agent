"""macOS-specific foreground + mic-active queries for the armed model.

Exports mirror ``platform_win.py``:

- ``is_mic_active()`` → bool, from ``kAudioDevicePropertyDeviceIsRunningSomewhere``
  on the default input device. True if any process is currently capturing.
- ``get_running_processes()`` → set of psutil-visible process names / bundle
  ids (for the macOS proxy path in the matcher).
- ``get_foreground_info()`` → frontmost bundle id + (if browser) active tab
  URL via AppleScript. AppleScript is cached for 2 s per browser.
- ``get_mic_holders()`` → always ``[]`` on macOS; we can't attribute
  mic-in-use to a specific process cheaply. Kept for interface symmetry.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Optional

from ..config import DetectorSpec
from .detectors import BROWSER_PROCESS_NAMES, ForegroundInfo, MicHolder

log = logging.getLogger(__name__)


_BROWSER_BUNDLES = {
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.brave.Browser": "Brave Browser",
    "company.thebrowser.Browser": "Arc",
    "org.mozilla.firefox": "Firefox",
    "com.operasoftware.Opera": "Opera",
    "com.vivaldi.Vivaldi": "Vivaldi",
}

# URL cache per browser so polling every 2 s doesn't spawn an osascript for
# every sample. {bundle_id: (url, timestamp)}
_URL_CACHE: dict[str, tuple[Optional[str], float]] = {}
_URL_CACHE_TTL_SECS = 2.0


def get_mic_holders() -> list[MicHolder]:
    """No per-process attribution on macOS. Callers rely on the combination of
    ``is_mic_active`` + ``get_running_processes`` + ``get_foreground_info`` for
    the ``mic_active_plus_running`` match source."""
    return []


# Known browser bundle ids. Used by ``resolve_pids_for_spec`` for browser
# specs so per-app system-audio capture can scope to the browser's PID tree
# even though we couldn't attribute the mic-hold to a specific browser.
# Parallel to ``detectors.BROWSER_PROCESS_NAMES`` (which covers Windows
# process executable names).
_BROWSER_BUNDLE_IDS = frozenset(_BROWSER_BUNDLES.keys())


def resolve_pids_for_spec(spec: "DetectorSpec") -> tuple[int, ...]:
    """Enumerate PIDs currently matching ``spec`` via psutil + NSWorkspace.

    Used on macOS (where ``MicHolder.pid`` is unavailable) to populate
    ``ArmReason.target_pids`` before arming. For browser specs we return
    the PIDs of every running browser process — per-tab scoping isn't
    possible without a browser extension, so all tabs in that browser's
    PID tree will be captured.

    Empty tuple on any error — caller treats empty as "fall back to
    endpoint-wide capture".
    """
    if sys.platform != "darwin":
        return ()

    pids: set[int] = set()
    target_bundles: set[str]
    target_names: set[str]
    if spec.is_browser:
        target_bundles = {b.lower() for b in _BROWSER_BUNDLE_IDS}
        target_names = {n.lower() for n in BROWSER_PROCESS_NAMES}
    else:
        target_bundles = {b.lower() for b in spec.bundle_ids}
        target_names = {p.lower() for p in spec.process_names}

    # NSWorkspace: bundle id → PID (cheap; already in-process).
    if target_bundles:
        try:
            from AppKit import NSWorkspace  # type: ignore[import-not-found]
            ws = NSWorkspace.sharedWorkspace()
            for app in ws.runningApplications():
                try:
                    bid = app.bundleIdentifier()
                    if not bid:
                        continue
                    if str(bid).lower() not in target_bundles:
                        continue
                    pid = int(app.processIdentifier() or 0)
                    if pid > 0:
                        pids.add(pid)
                except Exception:
                    continue
        except Exception:
            log.debug(
                "[arm.mac] NSWorkspace PID enumeration failed for %s",
                spec.app_key,
                exc_info=True,
            )

    # psutil fallback: process name → PID. Catches non-bundled CLI-style
    # helpers (e.g. zoom auxiliary processes) that NSWorkspace doesn't
    # surface as NSRunningApplication entries.
    if target_names:
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if not name or name not in target_names:
                        continue
                    pid = int(p.info.get("pid") or p.pid or 0)
                    if pid > 0:
                        pids.add(pid)
                except Exception:
                    continue
        except Exception:
            log.debug(
                "[arm.mac] psutil PID enumeration failed for %s",
                spec.app_key,
                exc_info=True,
            )

    return tuple(sorted(pids))


def get_browser_window_titles() -> list[str]:
    """macOS: no cheap way to enumerate all browser window titles.

    Per-window Apple Events (``get title of every window of application …``)
    would require Automation permission per-browser and would slow the
    watcher poll to hundreds of ms. For v1 we rely on the foreground
    browser's tab URL/title already populated in ``get_foreground_info``;
    the matcher's title-pattern path still works when the user is focused
    on the browser. Returns empty otherwise.
    """
    return []


def get_browser_window_urls() -> list[str]:
    """macOS: stub. The foreground browser's active-tab URL is already
    populated on ``ForegroundInfo.browser_tab_url`` via AppleScript in
    ``get_foreground_info``. Enumerating URLs of every window of every
    browser would require the same per-browser Automation permission plus
    multiple osascript spawns per poll — deferred until a macOS user
    reports an Alt+Tab-equivalent mis-match.
    """
    return []


def is_mic_active() -> bool:
    """Is any process currently capturing from the default input device?

    Queries ``kAudioDevicePropertyDeviceIsRunningSomewhere`` via pyobjc's
    CoreAudio bindings. Returns False on any error (permission denied,
    missing framework, device absent).
    """
    if sys.platform != "darwin":
        return False
    try:
        import CoreAudio  # type: ignore[import-not-found]
    except Exception:
        log.debug("[arm.mac] CoreAudio framework unavailable", exc_info=True)
        return False

    # NOTE: the exact CoreAudio property selector / struct types differ between
    # pyobjc-framework-CoreAudio versions. The implementation sketch below is
    # the shape we want; the ArmController tolerates this returning False so
    # any binding incompatibility degrades to "no mic signal" rather than
    # crashing. Follow-up: pin an exact pyobjc recipe after real-Mac verification
    # (see deferred-work memory project_deferred_work.md).
    try:
        prop = CoreAudio.AudioObjectPropertyAddress(
            mSelector=CoreAudio.kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope=CoreAudio.kAudioObjectPropertyScopeGlobal,
            mElement=CoreAudio.kAudioObjectPropertyElementMaster,
        )
        # Default input device id.
        sys_prop = CoreAudio.AudioObjectPropertyAddress(
            mSelector=CoreAudio.kAudioHardwarePropertyDefaultInputDevice,
            mScope=CoreAudio.kAudioObjectPropertyScopeGlobal,
            mElement=CoreAudio.kAudioObjectPropertyElementMaster,
        )
        default_id_ptr = (CoreAudio.UInt32 * 1)(0)
        size_ptr = (CoreAudio.UInt32 * 1)(4)
        err = CoreAudio.AudioObjectGetPropertyData(
            CoreAudio.kAudioObjectSystemObject,
            sys_prop, 0, None, size_ptr, default_id_ptr,
        )
        if err != 0:
            return False
        default_id = default_id_ptr[0]
        running_ptr = (CoreAudio.UInt32 * 1)(0)
        err = CoreAudio.AudioObjectGetPropertyData(
            default_id, prop, 0, None, size_ptr, running_ptr,
        )
        if err != 0:
            return False
        return bool(running_ptr[0])
    except Exception:
        log.debug("[arm.mac] is_mic_active query failed", exc_info=True)
        return False


def get_running_processes() -> frozenset[str]:
    """Return lowercased psutil process names + known bundle ids.

    The matcher checks both sets (process name OR bundle id) when evaluating
    the ``mic_active_plus_running`` match source. psutil gives us names
    cheaply; bundle ids for GUI apps come from NSWorkspace.runningApplications.
    """
    names: set[str] = set()
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                n = p.info.get("name")
                if n:
                    names.add(n.lower())
            except Exception:
                continue
    except Exception:
        log.debug("[arm.mac] psutil process iter failed", exc_info=True)

    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        ws = NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            try:
                bid = app.bundleIdentifier()
                if bid:
                    names.add(str(bid).lower())
            except Exception:
                continue
    except Exception:
        log.debug("[arm.mac] NSWorkspace running apps failed", exc_info=True)

    return frozenset(names)


def get_foreground_info() -> ForegroundInfo:
    """Frontmost bundle id + (for browsers) active tab URL."""
    if sys.platform != "darwin":
        return ForegroundInfo()
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except Exception:
        log.debug("[arm.mac] AppKit unavailable", exc_info=True)
        return ForegroundInfo()

    try:
        ws = NSWorkspace.sharedWorkspace()
        front = ws.frontmostApplication()
        if front is None:
            return ForegroundInfo()
        bundle_id = str(front.bundleIdentifier() or "") or None
        proc_name = str(front.localizedName() or "") or None
    except Exception:
        log.debug("[arm.mac] frontmostApplication query failed", exc_info=True)
        return ForegroundInfo()

    is_browser = bool(bundle_id and bundle_id in _BROWSER_BUNDLES)
    url: Optional[str] = None
    if is_browser and bundle_id:
        url = _get_browser_url_cached(bundle_id)

    return ForegroundInfo(
        process_name=proc_name,
        bundle_id=bundle_id,
        window_title=None,
        browser_tab_url=url,
        browser_tab_title=None,
        is_browser=is_browser,
    )


def _get_browser_url_cached(bundle_id: str) -> Optional[str]:
    now = time.monotonic()
    entry = _URL_CACHE.get(bundle_id)
    if entry is not None and (now - entry[1]) < _URL_CACHE_TTL_SECS:
        return entry[0]
    url = _get_browser_url_fresh(bundle_id)
    _URL_CACHE[bundle_id] = (url, now)
    return url


def _get_browser_url_fresh(bundle_id: str) -> Optional[str]:
    """Run an AppleScript to read the active tab URL. Timeout 500 ms.

    Returns None on error — the Automation permission might not be granted,
    the browser might be in a state without a front window, or the browser
    might not support AppleScript (Firefox). Callers handle None as
    "no URL available, fall back to title-regex matching".
    """
    app_name = _BROWSER_BUNDLES.get(bundle_id)
    if not app_name:
        return None

    if bundle_id == "com.apple.Safari":
        script = (
            f'tell application "{app_name}" to get URL of current tab of front window'
        )
    else:
        # Chrome / Edge / Brave / Arc / Opera / Vivaldi all accept this shape.
        script = (
            f'tell application "{app_name}" to get URL of active tab of front window'
        )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=0.5,
        )
    except Exception:
        log.debug("[arm.mac] osascript spawn failed", exc_info=True)
        return None
    if proc.returncode != 0:
        log.debug("[arm.mac] osascript non-zero: %r", proc.stderr)
        return None
    url = proc.stdout.strip()
    return url or None
