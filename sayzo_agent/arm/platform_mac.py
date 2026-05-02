"""macOS-specific foreground + mic-active queries for the armed model.

Exports mirror ``platform_win.py``:

- ``is_mic_active()`` → bool, from ``kAudioDevicePropertyDeviceIsRunningSomewhere``
  on the default input device. True if any process is currently capturing.
- ``get_running_processes()`` → set of psutil-visible process names / bundle
  ids (for the macOS proxy path in the matcher).
- ``get_foreground_info()`` → frontmost bundle id + (if browser) active tab
  title via the Accessibility API. Cached for 2 s per browser.
- ``get_browser_window_titles()`` → titles of every visible browser window
  across all running browsers, also via Accessibility.
- ``get_mic_holders()`` → always ``[]`` on macOS; we can't attribute
  mic-in-use to a specific process cheaply. Kept for interface symmetry.

**Why AX, not AppleScript:** earlier versions read browser tab URLs via
``osascript`` which forced macOS to fire the Automation TCC dialog
("Sayzo wants to control your browser") — alarming wording for a coaching
app, since the OS has no softer phrasing for "read which page is open."
Switching to ``AXUIElementCopyAttributeValue`` reuses the Accessibility
permission already required for the global hotkey, so no additional TCC
prompt ever appears. AX returns the window title (e.g. ``"Meet -
abc-defg-hij - Google Chrome"``) which the matcher's ``title_patterns``
regexes already handle the same way they do on Windows.
"""
from __future__ import annotations

import ctypes
import logging
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

# Title cache per browser bundle so polling every 2 s doesn't re-walk the
# AX tree on every sample. {bundle_id: (titles, timestamp)}
_TITLES_CACHE: dict[str, tuple[list[str], float]] = {}
_TITLES_CACHE_TTL_SECS = 2.0


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


def _pids_for_bundle(bundle_id: str) -> list[int]:
    """Return PIDs of every running process matching ``bundle_id``.

    Used by the AX title reader to walk every browser instance the user
    has open (a single Chrome session can have multiple windows under one
    PID, but profile-isolated launches show up as multiple PIDs).
    """
    pids: list[int] = []
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        ws = NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            try:
                bid = app.bundleIdentifier()
                if bid and str(bid) == bundle_id:
                    pid = int(app.processIdentifier() or 0)
                    if pid > 0:
                        pids.append(pid)
            except Exception:
                continue
    except Exception:
        log.debug(
            "[arm.mac] PID lookup for %s failed", bundle_id, exc_info=True
        )
    return pids


def _get_browser_titles_fresh(bundle_id: str) -> list[str]:
    """Walk a browser's AX tree to read every visible window's title.

    Uses ``AXUIElementCopyAttributeValue`` against ``kAXWindowsAttribute`` /
    ``kAXTitleAttribute``. The Accessibility TCC permission already required
    for the global hotkey listener (``pynput`` ⇒ ``CGEventTap``) covers this
    call too — no separate Automation prompt fires.

    If Accessibility isn't granted, the AX call returns
    ``kAXErrorAPIDisabled`` (-25211) silently — no dialog, no exception. We
    treat any error as "no titles" so the matcher falls through to
    title-pattern misses. Same fallback path Windows takes when
    UIAutomation can't read a particular tab.
    """
    pids = _pids_for_bundle(bundle_id)
    if not pids:
        return []

    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            kAXWindowsAttribute,
            kAXTitleAttribute,
        )
    except Exception:
        log.debug(
            "[arm.mac] ApplicationServices AX bindings unavailable",
            exc_info=True,
        )
        return []

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
            try:
                title_str = str(title).strip()
            except Exception:
                continue
            if title_str:
                titles.append(title_str)
    return titles


def _get_browser_titles_cached(bundle_id: str) -> list[str]:
    now = time.monotonic()
    entry = _TITLES_CACHE.get(bundle_id)
    if entry is not None and (now - entry[1]) < _TITLES_CACHE_TTL_SECS:
        return entry[0]
    titles = _get_browser_titles_fresh(bundle_id)
    _TITLES_CACHE[bundle_id] = (titles, now)
    return titles


def get_browser_window_titles() -> list[str]:
    """Aggregate titles from every running browser via the Accessibility API.

    Mirrors the Windows ``platform_win.get_browser_window_titles``
    behaviour so ``ForegroundInfo.browser_window_titles`` is populated even
    when the user has Alt+Tab'd away from the browser holding the mic.
    Returns ``[]`` when Accessibility isn't granted or pyobjc bindings
    aren't loadable — the matcher gracefully degrades to "no title-pattern
    match available".
    """
    if sys.platform != "darwin":
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for bundle_id in _BROWSER_BUNDLES:
        for t in _get_browser_titles_cached(bundle_id):
            if t in seen:
                continue
            seen.add(t)
            titles.append(t)
    return titles


def get_browser_window_urls() -> list[str]:
    """macOS no longer reads tab URLs (would require the Automation TCC
    dialog "Sayzo wants to control your browser", which we explicitly
    avoid). Title-based matching via ``get_browser_window_titles`` plus
    ``DetectorSpec.title_patterns`` covers web meeting detection on macOS.
    Always returns ``[]``.
    """
    return []


# CoreAudio FourCC selectors (from <CoreAudio/AudioHardwareBase.h>). Defined
# inline so we don't depend on pyobjc-framework-CoreAudio re-exporting them
# under a stable Python name — see ``_load_core_audio`` for why we abandoned
# pyobjc here.
_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDefaultInputDevice = 0x64496E20  # 'dIn '
_kAudioObjectPropertyScopeGlobal = 0x676C6F62  # 'glob'
# kAudioObjectPropertyElementMain (renamed from ElementMaster in macOS 12);
# both names are 0 — the constant value never changed.
_kAudioObjectPropertyElementMain = 0
_kAudioDevicePropertyDeviceIsRunningSomewhere = 0x676F696E  # 'goin'


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_CORE_AUDIO_LIB: Optional[ctypes.CDLL] = None
_CORE_AUDIO_LOAD_FAILED = False


def _load_core_audio() -> Optional[ctypes.CDLL]:
    """Lazy-load the CoreAudio framework via ctypes and bind argtypes once.

    pyobjc-framework-CoreAudio was previously used here, but its bindings
    don't expose the ``UInt32`` integer type the way our caller assumed
    (``CoreAudio.UInt32`` raises AttributeError from pyobjc's lazy-import
    layer). Rather than chase pyobjc binding shape across versions, we
    call CoreAudio directly: the C ABI is stable since OS X 10.6 and
    ``AudioObjectGetPropertyData`` takes only ``UInt32`` / pointer args
    that ctypes models exactly.
    """
    global _CORE_AUDIO_LIB, _CORE_AUDIO_LOAD_FAILED
    if _CORE_AUDIO_LIB is not None:
        return _CORE_AUDIO_LIB
    if _CORE_AUDIO_LOAD_FAILED:
        return None
    try:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
        lib.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,                              # AudioObjectID
            ctypes.POINTER(_AudioObjectPropertyAddress),  # address
            ctypes.c_uint32,                              # qualifierDataSize
            ctypes.c_void_p,                              # qualifierData
            ctypes.POINTER(ctypes.c_uint32),              # ioDataSize
            ctypes.c_void_p,                              # outData
        ]
        lib.AudioObjectGetPropertyData.restype = ctypes.c_int32  # OSStatus
    except Exception:
        _CORE_AUDIO_LOAD_FAILED = True
        log.warning(
            "[arm.mac] CoreAudio framework unavailable — is_mic_active will "
            "always return False (whitelist watcher will never fire)",
            exc_info=True,
        )
        return None
    _CORE_AUDIO_LIB = lib
    return lib


def is_mic_active() -> bool:
    """Is any process currently capturing from the default input device?

    Queries ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on the default
    input device via the CoreAudio C API (loaded with ctypes; pyobjc is
    not used here — see ``_load_core_audio``). Returns False on any error
    (framework missing, no default input, OS denied the call).
    """
    if sys.platform != "darwin":
        return False
    lib = _load_core_audio()
    if lib is None:
        return False

    try:
        # Step 1: read the default input device's AudioObjectID.
        addr_default = _AudioObjectPropertyAddress(
            _kAudioHardwarePropertyDefaultInputDevice,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        default_id = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        err = lib.AudioObjectGetPropertyData(
            _kAudioObjectSystemObject,
            ctypes.byref(addr_default),
            0, None,
            ctypes.byref(size), ctypes.byref(default_id),
        )
        if err != 0:
            _warn_mic_active_once(
                "AudioObjectGetPropertyData(default-input) returned err=%d", err,
            )
            return False
        if default_id.value == 0:
            # 0 means "no default input device" — treat as inactive.
            return False

        # Step 2: read kAudioDevicePropertyDeviceIsRunningSomewhere on it.
        addr_running = _AudioObjectPropertyAddress(
            _kAudioDevicePropertyDeviceIsRunningSomewhere,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        running = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        err = lib.AudioObjectGetPropertyData(
            default_id.value,
            ctypes.byref(addr_running),
            0, None,
            ctypes.byref(size), ctypes.byref(running),
        )
        if err != 0:
            _warn_mic_active_once(
                "AudioObjectGetPropertyData(is-running) returned err=%d", err,
            )
            return False
        return bool(running.value)
    except Exception as exc:
        _warn_mic_active_once(
            "is_mic_active CoreAudio call raised: %s",
            exc,
            exc_info=True,
        )
        return False


_MIC_ACTIVE_WARN_FIRED = False


def _warn_mic_active_once(fmt: str, *args: object, exc_info: bool = False) -> None:
    """Log a one-shot warning the first time ``is_mic_active`` hits a
    failure path, then drop to debug for subsequent identical hits.

    Without this throttle we'd spam agent.log at the watcher's poll
    cadence (every 2 s) every time the CoreAudio binding is broken;
    one warning per agent run is enough to surface the issue.
    """
    global _MIC_ACTIVE_WARN_FIRED
    if _MIC_ACTIVE_WARN_FIRED:
        log.debug("[arm.mac] " + fmt, *args, exc_info=exc_info)
        return
    _MIC_ACTIVE_WARN_FIRED = True
    log.warning(
        "[arm.mac] " + fmt + " — whitelist watcher won't see mic-active "
        "until this is resolved",
        *args,
        exc_info=exc_info,
    )


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
    """Frontmost bundle id + (for browsers) frontmost window title via AX."""
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
    tab_title: Optional[str] = None
    if is_browser and bundle_id:
        # AX returns windows in z-order — the frontmost one first.
        cached = _get_browser_titles_cached(bundle_id)
        tab_title = cached[0] if cached else None

    return ForegroundInfo(
        process_name=proc_name,
        bundle_id=bundle_id,
        window_title=None,
        browser_tab_url=None,
        browser_tab_title=tab_title,
        is_browser=is_browser,
    )


