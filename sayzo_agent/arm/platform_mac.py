"""macOS-specific foreground + mic-holder queries for the armed model.

Exports mirror ``platform_win.py``:

- ``get_mic_holders()`` → list of :class:`MicHolder` for every process
  currently capturing the microphone, attributed to its user-facing app
  via Apple's responsibility SPI. Powered by the ``audio-detect`` Swift
  helper (``arm/audio-detect/main.swift``); see :mod:`audio_detect`.
- ``is_mic_active()`` → bool, True if any process is currently capturing.
  Derived from ``get_mic_holders()`` so it stays consistent with the
  per-process attribution path.
- ``get_running_processes()`` → set of psutil + NSWorkspace bundle ids.
  Kept for the unmatched-holders "Suggested to add" recording path.
- ``get_foreground_info()`` → frontmost bundle id + (if browser) active
  tab title via the Accessibility API. Cached for 2 s per browser.
- ``get_browser_window_titles()`` → titles of every visible browser window
  across all running browsers, also via Accessibility.
- ``get_browser_window_urls()`` → active-tab URLs via Accessibility for
  browsers that expose them (Safari yes, Chrome no — handled gracefully
  via title-pattern fallback in the matcher).

**The macOS rewrite (v2.5+).** Up to v2.4.x this module returned
``[]`` from ``get_mic_holders`` and relied on a foreground-coupled proxy
in :mod:`detectors` (``mic_active_plus_running``). That proxy required
the meeting app to be the frontmost window for matching to fire — Alt-
tabbing away from Zoom would silently drop detection. The Swift helper
removes that constraint and brings macOS to Windows-equivalent
behaviour: any process holding the mic is detected regardless of
foreground state.

**Why AX, not AppleScript:** earlier versions read browser tab URLs via
``osascript`` which forced macOS to fire the Automation TCC dialog
("Sayzo wants to control your browser") — alarming wording for a
coaching app, since the OS has no softer phrasing for "read which page
is open." Switching to ``AXUIElementCopyAttributeValue`` reuses the
Accessibility permission already required for the global hotkey, so no
additional TCC prompt ever appears. AX returns the window title
(e.g. ``"Meet - abc-defg-hij - Google Chrome"``) plus the active tab
URL on browsers that expose it.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Optional

import psutil

from . import audio_detect
from .detectors import ForegroundInfo, MicHolder

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


# Browser audio happens in helper processes whose bundle id can either be
# under the browser's own namespace (``com.google.Chrome.helper.gpu``)
# or — for Safari + WebKit-derived embeddings — under
# ``com.apple.webkit.*``. The responsibility SPI usually maps these back
# to the user-facing browser, but on Safari it sometimes returns the
# helper itself (the parent chain is launchd-rooted, no link back). This
# table is the defensive fallback for the WebKit case.
#
# Key: helper-bundle-id prefix (lowercase). Value: owning browser bundle id.
# Lookup is ``startswith(prefix)`` per entry, so order doesn't matter.
_BROWSER_HELPER_PREFIXES: dict[str, str] = {
    # WebKit framework helpers (Safari and any WebKit-embedding browser).
    "com.apple.webkit.": "com.apple.Safari",
    # Chromium-derived browsers — all use ``<browser-bundle>.helper.*``.
    "com.google.chrome.helper": "com.google.Chrome",
    "com.microsoft.edgemac.helper": "com.microsoft.edgemac",
    "com.brave.browser.helper": "com.brave.Browser",
    "company.thebrowser.browser.helper": "company.thebrowser.Browser",
    "com.operasoftware.opera.helper": "com.operasoftware.Opera",
    "com.vivaldi.vivaldi.helper": "com.vivaldi.Vivaldi",
    # Firefox: WebRTC audio runs in plugin-container; parent IS Firefox so
    # parent-walking would also work, but prefix matching is uniform.
    "org.mozilla.firefox.plugin-container": "org.mozilla.firefox",
}


# Per-browser AX cache. One entry per bundle id holds (titles, urls) +
# the last-fresh timestamp so the watcher's 2 s poll doesn't re-walk
# the AX tree on every sample.
_AX_CACHE: dict[str, tuple[tuple[list[str], list[str]], float]] = {}
_AX_CACHE_TTL_SECS = 2.0


def _iter_running_apps() -> list[tuple[int, str]]:
    """Snapshot ``NSWorkspace.runningApplications()`` once and return a
    flat list of ``(pid, bundle_id)`` for every app with both fields set.

    The four lookup helpers below (`_bundle_for_pid`,
    `_running_pid_for_bundle`, `_pids_for_bundle`, plus the bundle
    branch of `resolve_pids_for_spec`) used to each open this same
    iterator independently — meaning a single `get_mic_holders()` call
    could walk the runningApplications list 6+ times for an N-helper
    capture set. Materializing once and reusing avoids that.
    """
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except Exception:
        log.debug("[arm.mac] AppKit unavailable", exc_info=True)
        return []
    out: list[tuple[int, str]] = []
    try:
        ws = NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            try:
                bid = app.bundleIdentifier()
                pid = int(app.processIdentifier() or 0)
            except Exception:
                continue
            if not bid or pid <= 0:
                continue
            out.append((pid, str(bid)))
    except Exception:
        log.debug("[arm.mac] NSWorkspace.runningApplications failed", exc_info=True)
    return out


def _bundle_by_pid_map() -> dict[int, str]:
    return {pid: bid for pid, bid in _iter_running_apps()}


def _pids_by_bundle_map() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for pid, bid in _iter_running_apps():
        out.setdefault(bid, []).append(pid)
    return out


def _browser_for_helper_bundle(bundle: Optional[str]) -> Optional[str]:
    """If ``bundle`` is a known browser-helper bundle id, return the
    owning browser's bundle id. Else None.

    Used by :func:`get_mic_holders` as a fallback for the WebKit case
    where the responsibility SPI returns the helper itself.
    """
    if not bundle:
        return None
    bl = bundle.lower()
    for prefix, browser in _BROWSER_HELPER_PREFIXES.items():
        if bl.startswith(prefix):
            return browser
    return None


def _bundle_for_pid(pid: int, *, bundle_by_pid: Optional[dict[int, str]] = None) -> Optional[str]:
    """Return the NSRunningApplication bundle id for ``pid``, or None.

    Helpers / services typically don't have an NSRunningApplication entry
    — they live in a separate launchd domain and only the user-facing
    GUI app appears in this list. So a None return means "this PID is a
    helper, walk up to find the responsible app."

    ``bundle_by_pid`` lets callers in a hot loop pre-materialize the map
    once via :func:`_bundle_by_pid_map` and pass it in, avoiding O(N)
    re-walks of NSWorkspace per parent-walk hop.
    """
    if bundle_by_pid is not None:
        return bundle_by_pid.get(pid)
    return _bundle_by_pid_map().get(pid)


def _running_pid_for_bundle(bundle: str, *, pids_by_bundle: Optional[dict[str, list[int]]] = None) -> Optional[int]:
    """First running NSRunningApplication PID for ``bundle``, or None.

    Used by the WebKit fallback: when the SPI returns a helper PID we
    can't introspect, we know the browser bundle it belongs to and need
    to find a PID for the actual browser process so the system-audio
    capture path can scope correctly.
    """
    src = pids_by_bundle if pids_by_bundle is not None else _pids_by_bundle_map()
    pids = src.get(bundle)
    return pids[0] if pids else None


def _walk_to_gui_ancestor(
    start_pid: int,
    *,
    bundle_by_pid: dict[int, str],
    max_depth: int = 6,
) -> tuple[Optional[int], Optional[str]]:
    """Walk parent PIDs from ``start_pid`` until we hit a process with an
    NSRunningApplication entry (a real user-facing GUI app).

    Used to recover the owning app when the responsibility SPI lands on
    an intermediate helper. Discord's audio path is
    ``Discord.helper.Renderer → Discord Helper → Discord``; the SPI
    returns the middle helper, which has no NSRunningApplication entry,
    and we walk one more parent up to find Discord itself.

    Returns (owner_pid, bundle_id) or (None, None) if the walk falls off
    into launchd / kernel without finding a GUI ancestor.
    """
    cur = start_pid
    for _ in range(max_depth):
        bid = bundle_by_pid.get(cur)
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


def _resolve_owner_with_bundle(
    *,
    pid: int,
    responsible_pid: int,
    bundle_id: Optional[str],
    bundle_by_pid: dict[int, str],
    pids_by_bundle: dict[str, list[int]],
) -> tuple[Optional[int], Optional[str]]:
    """Map an audio process (pid + responsible_pid + maybe-known bundle id)
    to its user-facing (PID, bundle_id).

    Resolution order:

      1. Responsibility SPI (already done in Swift) → walk to GUI ancestor.
         Apple's own privacy attribution; what the orange privacy
         indicator uses.
      2. Browser-helper bundle prefix → look up the browser's main PID via
         NSRunningApplication. Defensive fallback for the WebKit case.
      3. Plain parent-walk from the capturing PID. Catches anything (1)
         and (2) didn't reach.

    Returns (owner_pid, owner_bundle) or (None, None) if no GUI owner
    could be resolved.
    """
    if responsible_pid > 0:
        owner_pid, bid = _walk_to_gui_ancestor(
            responsible_pid, bundle_by_pid=bundle_by_pid,
        )
        if bid is not None:
            return owner_pid, bid

    browser_bundle = _browser_for_helper_bundle(bundle_id)
    if browser_bundle is not None:
        pids = pids_by_bundle.get(browser_bundle)
        return (pids[0] if pids else None), browser_bundle

    if pid > 0:
        owner_pid, bid = _walk_to_gui_ancestor(
            pid, bundle_by_pid=bundle_by_pid,
        )
        if bid is not None:
            return owner_pid, bid

    return None, None


_empty_holders_log_signature: Optional[tuple] = None


def _warn_empty_holders_once(capturing: list) -> None:
    """Log a one-shot INFO summary of audio_detect.snapshot when it has
    input-active processes but get_mic_holders couldn't produce any
    attributable holders.

    Dedup'd on the (pid, bundle_id) shape of the snapshot so a long
    meeting in the same broken state doesn't spam the watcher's 2 s
    poll cadence.
    """
    global _empty_holders_log_signature
    sig = tuple(sorted((p.pid, p.responsible_pid, p.bundle_id) for p in capturing))
    if sig == _empty_holders_log_signature:
        return
    _empty_holders_log_signature = sig
    summary = ", ".join(
        f"pid={p.pid}/resp={p.responsible_pid}/bundle={p.bundle_id!r}"
        for p in capturing
    )
    log.info(
        "[arm.mac] audio_detect saw %d capturing process(es) but produced "
        "0 holders — entries: %s",
        len(capturing), summary,
    )


def get_mic_holders() -> list[MicHolder]:
    """Return one :class:`MicHolder` per process currently capturing the mic.

    Each holder carries the user-facing app's PID and bundle id (NOT the
    helper's), resolved via Apple's responsibility SPI. ``process_name``
    is set to the bundle id for parity with how the matcher looks up
    desktop specs (which carry both ``process_names`` and ``bundle_ids``).

    Empty list when nothing is capturing, when the Swift helper is
    missing, or when the per-process API isn't available on this OS.
    The matcher tolerates an empty list — it just means no whitelist
    match fires that poll.
    """
    if sys.platform != "darwin":
        return []

    snapshot = audio_detect.snapshot()
    capturing = [p for p in snapshot if p.input]
    if not capturing:
        return []

    # One NSWorkspace walk per get_mic_holders() call, shared across every
    # capturing process's parent-walk + browser-helper resolution.
    bundle_by_pid: dict[int, str] = {}
    pids_by_bundle: dict[str, list[int]] = {}
    for pid, bid in _iter_running_apps():
        bundle_by_pid[pid] = bid
        pids_by_bundle.setdefault(bid, []).append(pid)

    # Mirrors scripts/mac_probes/08_full_detect.py — the probe the user
    # validated end-to-end. Two important properties:
    #
    # 1. Trust ``owner_bundle`` from _resolve_owner even when
    #    ``owner_pid`` is None. Pass 2 of _resolve_owner (browser-helper
    #    prefix) can return ("com.apple.Safari", None) when NSWorkspace
    #    doesn't have a PID for Safari yet but we still know the helper
    #    belongs to it. The matcher gates on bundle_id, not pid; the
    #    helper pid is fine as a placeholder for system-audio scoping.
    # 2. When _resolve_owner returns (None, None) entirely, surface the
    #    raw capturing-process bundle as a last resort. The matcher's
    #    helper-bundle-prefix backstop in detectors.py will still catch
    #    things like ``com.hnc.Discord.helper.Renderer`` → discord spec.
    holders: list[MicHolder] = []
    seen_keys: set[tuple[int, str]] = set()

    for proc in capturing:
        # Recover bundle_id from NSWorkspace if the Swift helper didn't
        # populate it. We've seen production cases where the bundled
        # binary's CFString reads return null while NSWorkspace's bundle
        # lookup by PID still works fine for the same processes
        # (different API surface, different gating). Prefer the
        # NSWorkspace value over null so _resolve_owner has something to
        # work with.
        recovered_bundle = proc.bundle_id or bundle_by_pid.get(proc.pid)

        owner_pid, owner_bundle = _resolve_owner_with_bundle(
            pid=proc.pid,
            responsible_pid=proc.responsible_pid,
            bundle_id=recovered_bundle,
            bundle_by_pid=bundle_by_pid,
            pids_by_bundle=pids_by_bundle,
        )
        if owner_bundle is not None:
            pid = owner_pid if owner_pid is not None else proc.pid
            key = (pid, owner_bundle.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            holders.append(MicHolder(
                process_name=owner_bundle,
                pid=pid,
                bundle_id=owner_bundle,
            ))
            continue

        # No attribution worked — surface the recovered bundle (or
        # raw helper bundle) so the matcher's helper-bundle-prefix
        # backstop has a chance.
        if not recovered_bundle or proc.pid <= 0:
            continue
        key = (proc.pid, recovered_bundle.lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        holders.append(MicHolder(
            process_name=recovered_bundle,
            pid=proc.pid,
            bundle_id=recovered_bundle,
        ))

    # One-shot diagnostic when audio_detect saw input-active processes
    # but we produced zero holders. Lets a user paste a normal log and
    # have us see exactly what the Swift helper returned.
    if not holders:
        _warn_empty_holders_once(capturing)

    return holders


def is_mic_active() -> bool:
    """True if any process is currently capturing audio input.

    Derived from :func:`get_mic_holders` so the system-wide bit stays
    consistent with the per-process attribution. Also picks up captures
    from processes we couldn't attribute to a GUI owner — important for
    the seen-apps recorder which should fire when ANY mic activity is
    happening, not only when we matched it.
    """
    if sys.platform != "darwin":
        return False
    if get_mic_holders():
        return True
    # Holders can be empty even when the mic is in use — e.g. a system
    # service captured but we couldn't walk to a GUI owner. Fall back to
    # checking if ANY audio process has IsRunningInput=YES.
    for proc in audio_detect.snapshot():
        if proc.input:
            return True
    return False


def resolve_pids_for_spec(spec) -> tuple[int, ...]:
    """Enumerate PIDs currently matching ``spec`` for system-audio scoping.

    Used by the ArmController to populate ``ArmReason.target_pids``
    before arming. Two paths:

    1. **Spec is a desktop app**: prefer PIDs that are currently
       capturing the mic (from :func:`get_mic_holders`) — those are
       guaranteed live and matched. If none match, fall back to
       enumerating every PID for the spec's bundle ids via NSWorkspace.
    2. **Spec is a browser**: enumerate every PID for the spec's
       browser bundle. Per-tab scoping isn't possible without an
       extension; all tabs share the browser's PID tree.

    Empty tuple ⇒ caller falls back to endpoint-wide capture.
    """
    if sys.platform != "darwin":
        return ()

    pids: set[int] = set()

    # Desktop spec: match against current mic-holders first (high signal).
    if not spec.is_browser:
        spec_bundles = {b.lower() for b in spec.bundle_ids}
        spec_names = {p.lower() for p in spec.process_names}
        for holder in get_mic_holders():
            if holder.bundle_id and holder.bundle_id.lower() in spec_bundles:
                if holder.pid > 0:
                    pids.add(holder.pid)
            elif holder.process_name and holder.process_name.lower() in spec_names:
                if holder.pid > 0:
                    pids.add(holder.pid)
        if pids:
            return tuple(sorted(pids))

    # Fallback / browser path: enumerate every NSRunningApplication
    # whose bundle id matches.
    target_bundles: set[str]
    if spec.is_browser:
        target_bundles = {b.lower() for b in _BROWSER_BUNDLES.keys()}
    else:
        target_bundles = {b.lower() for b in spec.bundle_ids}

    if target_bundles:
        for pid, bid in _iter_running_apps():
            if bid.lower() in target_bundles:
                pids.add(pid)

    # psutil fallback for non-bundled CLI helpers (e.g. zoom auxiliary
    # processes) that NSWorkspace doesn't surface.
    if not spec.is_browser and spec.process_names:
        try:
            target_names = {p.lower() for p in spec.process_names}
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
                spec.app_key, exc_info=True,
            )

    return tuple(sorted(pids))


# ---- AX title / URL reading (browser meeting detection) ------------------

def _pids_for_bundle(bundle_id: str) -> list[int]:
    """Return PIDs of every running process matching ``bundle_id``."""
    return [pid for pid, bid in _iter_running_apps() if bid == bundle_id]


def _ax_modules():
    """Lazy AX import. Returns None on macOS without ApplicationServices
    bindings — every consumer just degrades to empty results."""
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
    except Exception:
        log.debug("[arm.mac] ApplicationServices bindings unavailable", exc_info=True)
        return None
    return (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        kAXWindowsAttribute,
        kAXTitleAttribute,
        kAXChildrenAttribute,
        kAXRoleAttribute,
        kAXValueAttribute,
    )


def _get_browser_axdata_fresh(bundle_id: str) -> tuple[list[str], list[str]]:
    """One AX tree walk per browser; returns ``(titles, urls)``.

    For each window:
      - Title from ``kAXTitleAttribute`` on the window itself.
      - URL from ``AXURL`` on any ``AXWebArea`` descendant (Safari and
        the few other browsers that expose it for screen readers).
      - URL from ``AXValue`` on any ``AXTextField`` descendant whose
        value parses as a URL (omnibox text — backstop for browsers
        that don't expose AXWebArea.AXURL).

    Empirically (probe 05): Safari exposes both URL paths; Chrome
    exposes NEITHER without an Automation TCC dialog. So this returns
    populated URLs for Safari users and an empty URL list for Chrome
    users — the matcher's title-pattern fallback handles Chrome.

    Returns ``([], [])`` on any error (Accessibility not granted, AX
    bindings missing, no PIDs for this bundle). The matcher tolerates
    empty results — it just means no title- or URL-pattern match
    fires for that browser this poll.
    """
    pids = _pids_for_bundle(bundle_id)
    if not pids:
        return [], []
    mods = _ax_modules()
    if mods is None:
        return [], []
    (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
     kAXWindowsAttribute, kAXTitleAttribute,
     kAXChildrenAttribute, kAXRoleAttribute, kAXValueAttribute) = mods

    titles: list[str] = []
    urls: list[str] = []
    seen_urls: set[str] = set()

    def _read(elem, attr):
        try:
            err, value = AXUIElementCopyAttributeValue(elem, attr, None)
        except Exception:
            return None
        return None if err != 0 else value

    def _add_url(value: object) -> None:
        if value is None:
            return
        try:
            s = str(value).strip()
        except Exception:
            return
        if not s or s in seen_urls:
            return
        if not s.startswith(("http://", "https://", "file://")):
            return
        seen_urls.add(s)
        urls.append(s)

    def _walk(elem, depth: int = 0, max_depth: int = 8):
        yield elem
        if depth >= max_depth:
            return
        children = _read(elem, kAXChildrenAttribute)
        if not children:
            return
        for c in children:
            yield from _walk(c, depth + 1, max_depth)

    for pid in pids:
        try:
            app_ref = AXUIElementCreateApplication(pid)
        except Exception:
            continue
        windows = _read(app_ref, kAXWindowsAttribute) or []
        for window in windows:
            title = _read(window, kAXTitleAttribute)
            if title:
                try:
                    title_str = str(title).strip()
                except Exception:
                    title_str = ""
                if title_str:
                    titles.append(title_str)
            for elem in _walk(window):
                role = _read(elem, kAXRoleAttribute)
                if role == "AXWebArea":
                    _add_url(_read(elem, "AXURL"))
                elif role == "AXTextField":
                    _add_url(_read(elem, kAXValueAttribute))
    return titles, urls


def _get_browser_axdata_cached(bundle_id: str) -> tuple[list[str], list[str]]:
    """Cached per-bundle ``(titles, urls)``.

    The two consumer pairs (titles + URLs) used to walk the AX tree
    independently — one walk per bundle for titles, another for URLs.
    Combining halves the AX work in the watcher's hot path.
    """
    now = time.monotonic()
    entry = _AX_CACHE.get(bundle_id)
    if entry is not None and (now - entry[1]) < _AX_CACHE_TTL_SECS:
        return entry[0]
    data = _get_browser_axdata_fresh(bundle_id)
    _AX_CACHE[bundle_id] = (data, now)
    return data


def _get_browser_titles_cached(bundle_id: str) -> list[str]:
    return _get_browser_axdata_cached(bundle_id)[0]


def _get_browser_urls_cached(bundle_id: str) -> list[str]:
    return _get_browser_axdata_cached(bundle_id)[1]


def get_browser_window_titles() -> list[str]:
    """Aggregate titles from every running browser via the Accessibility API."""
    if sys.platform != "darwin":
        return []
    out: list[str] = []
    seen: set[str] = set()
    for bundle_id in _BROWSER_BUNDLES:
        for t in _get_browser_titles_cached(bundle_id):
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
    return out


def get_browser_window_urls() -> list[str]:
    """Aggregate active-tab URLs from every running browser via Accessibility.

    Used by the matcher so user-added URL detectors (the Settings → Web
    tab UX) match on Mac. Returns ``[]`` for browsers that don't expose
    URLs via AX (Chrome, mainly) — the matcher's title-pattern fallback
    handles those.
    """
    if sys.platform != "darwin":
        return []
    out: list[str] = []
    seen: set[str] = set()
    for bundle_id in _BROWSER_BUNDLES:
        for u in _get_browser_urls_cached(bundle_id):
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


def get_running_processes() -> frozenset[str]:
    """Return lowercased psutil process names + known bundle ids.

    Kept for the seen-apps recording path in the ArmController, which
    uses ``mic.running_processes`` to dedup repeat unknown holders.
    The matcher itself no longer consults this set — Pass 1
    (``mic_session``) now works on macOS via :func:`get_mic_holders`.
    """
    names: set[str] = set()
    try:
        for p in psutil.process_iter(["name"]):
            try:
                n = p.info.get("name")
                if n:
                    names.add(n.lower())
            except Exception:
                continue
    except Exception:
        log.debug("[arm.mac] psutil process iter failed", exc_info=True)

    for _pid, bid in _iter_running_apps():
        names.add(bid.lower())

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
    tab_url: Optional[str] = None
    if is_browser and bundle_id:
        # AX returns windows in z-order — the frontmost one first.
        cached_titles = _get_browser_titles_cached(bundle_id)
        tab_title = cached_titles[0] if cached_titles else None
        cached_urls = _get_browser_urls_cached(bundle_id)
        tab_url = cached_urls[0] if cached_urls else None

    return ForegroundInfo(
        process_name=proc_name,
        bundle_id=bundle_id,
        window_title=None,
        browser_tab_url=tab_url,
        browser_tab_title=tab_title,
        is_browser=is_browser,
    )
