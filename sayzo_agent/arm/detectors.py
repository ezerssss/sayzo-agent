"""Pure matching logic for the meeting-app whitelist.

The detector inputs come from platform-specific queries (see
``platform_win.py`` / ``platform_mac.py``): a list of processes
currently holding the default microphone as an active capture session,
plus the frontmost app + active browser tab URL/title. Both platforms
populate the same :class:`MicState` shape now — Windows from pycaw
WASAPI session enumeration, macOS from the ``audio-detect`` Swift
helper (per-process attribution via ``kAudioHardwarePropertyProcessObjectList``
on macOS 14.4+, mapped through the responsibility SPI to user-facing
apps).

This module is pure — no OS calls, no imports beyond the standard
library and config types. Tests drive it with synthetic
``ForegroundInfo`` / ``MicState`` inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from ..config import DetectorSpec


MatchSource = Literal[
    # Direct mic-holder match. Windows: pycaw WASAPI enumeration.
    # macOS (v2.5+): per-process attribution via the audio-detect Swift
    # helper, which maps helper PIDs back to the user-facing app via the
    # responsibility SPI.
    "mic_session",
    # Browser holds the mic AND the active tab URL/title matches a
    # DetectorSpec with is_browser=True.
    "browser_mic_plus_url",
]


@dataclass(frozen=True)
class ForegroundInfo:
    """Snapshot of the currently-frontmost application and (optionally) its
    browser tab info. Used for both the whitelist watcher and meeting-ended
    watcher. Platform-specific collection lives in ``platform_win.py`` /
    ``platform_mac.py``; this module just reads the fields."""

    # Windows: process executable name (e.g. "zoom.exe"). macOS: bundle id
    # (e.g. "us.zoom.xos"). Either may be None if the query failed.
    process_name: Optional[str] = None
    bundle_id: Optional[str] = None

    # Window title (Windows only; macOS reads tab-URL via AppleScript separately).
    window_title: Optional[str] = None

    # Browser tab info (macOS primarily — Windows is tab-title only for v1).
    browser_tab_url: Optional[str] = None
    browser_tab_title: Optional[str] = None

    # True if the frontmost process is a known browser.
    is_browser: bool = False

    # All visible top-level window titles owned by any browser process. On
    # Windows this is populated via ``platform_win.get_browser_window_titles``
    # so the matcher can find a Meet / Teams / Zoom-web tab even when the
    # user has Alt+Tab'd away from the browser. Empty on macOS (no cheap
    # enumeration without per-window Apple Events).
    browser_window_titles: tuple[str, ...] = field(default_factory=tuple)

    # Active-tab URLs for every visible browser window (Windows: read via
    # UIAutomation in ``platform_win.get_browser_window_urls``). Parallel
    # to ``browser_window_titles`` — needed so user-added URL detectors
    # (which only ship ``url_patterns``, no title_patterns) still match
    # when the browser isn't foreground. Empty on macOS: the active-tab
    # URL already populates ``browser_tab_url`` via AppleScript.
    browser_window_urls: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MicHolder:
    """One process currently holding an active capture session on the
    default microphone.

    - ``process_name`` — Windows: executable name (``zoom.exe``).
      macOS: bundle id of the user-facing app (``us.zoom.xos``); the
      capturing process may be a helper, but the holder is recorded
      against its responsible-app PID + bundle for matching.
    - ``pid`` — Windows: PID of the WASAPI capture session owner.
      macOS: PID of the user-facing GUI app (resolved from the
      capturing helper via the responsibility SPI).
    - ``bundle_id`` — macOS only; same value as ``process_name`` on
      Mac, ``None`` on Windows. Carried separately so the matcher can
      check it against ``DetectorSpec.bundle_ids`` without ambiguity.
    """

    process_name: str
    pid: int = -1
    bundle_id: Optional[str] = None


@dataclass(frozen=True)
class MicState:
    """Normalized mic-holder snapshot that works on both platforms.

    Windows: ``holders`` comes directly from WASAPI session enumeration
    via pycaw. macOS (v2.5+): ``holders`` comes from the ``audio-detect``
    Swift helper, which enumerates ``kAudioHardwarePropertyProcessObjectList``
    (macOS 14.4+) and maps each capturing process to its user-facing
    app via Apple's responsibility SPI. Either way the matcher only
    consults ``holders`` for the desktop match path.

    ``active`` and ``running_processes`` are still populated on macOS
    for the seen-apps recording path (catching unmatched mic-holders
    that didn't resolve to a known GUI app), but the matcher itself
    no longer uses them.
    """

    holders: list[MicHolder] = field(default_factory=list)
    active: bool = False
    running_processes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MatchResult:
    """A successful whitelist / meeting-ended match.

    - ``app_key`` is stable across sessions; used for cooldown bucketing
      and for the ArmController's ``_armed_for_app_key`` tracking.
    - ``display_name`` is user-facing; interpolated into toast copy.
    - ``source`` records which match path fired, for logging + tests.
    - ``target_pids`` is the set of PIDs the system-audio capture should
      scope to when armed for this match. On Windows this is populated
      directly from ``mic.holders`` (we have per-session PIDs). On macOS
      it's empty here — the ArmController fills it via a platform
      helper (psutil + NSWorkspace) before arming. Empty tuple means
      "fall back to endpoint-wide capture".
    """

    app_key: str
    display_name: str
    source: MatchSource
    target_pids: tuple[int, ...] = ()


_COMPILED_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


BROWSER_PROCESS_NAMES = frozenset({
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "arc.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "iexplore.exe",
})


def _browser_holds_mic(mic: MicState, foreground: ForegroundInfo) -> bool:
    """True if a browser process currently holds the mic.

    Both platforms now use direct ``mic.holders`` attribution:

    - Windows: pycaw WASAPI session ownership.
    - macOS (v2.5+): per-process attribution via the ``audio-detect``
      Swift helper, which already resolves browser-helper PIDs back to
      the user-facing browser via the responsibility SPI. So a Chrome
      audio helper capturing the mic shows up here as a holder with
      bundle id ``com.google.Chrome``.

    On macOS the holder's ``process_name`` field carries the bundle id
    (e.g. ``com.google.Chrome``), which won't match
    ``BROWSER_PROCESS_NAMES`` (Windows ``.exe`` names). We check
    ``bundle_id`` against the known browser bundle set as a parallel
    path. The :mod:`platform_mac` module owns that set; importing here
    would create a cycle, so we hardcode the same id list — both files
    must stay in sync if a new browser is added.
    """
    for h in mic.holders:
        if h.process_name.lower() in BROWSER_PROCESS_NAMES:
            return True
        if h.bundle_id and h.bundle_id in _BROWSER_BUNDLE_IDS:
            return True
    return False


# Mac browser bundles that count as "a browser is holding the mic" when
# attributed via the responsibility SPI. Mirrors
# ``platform_mac._BROWSER_BUNDLES`` keys; kept in sync manually because
# this is the pure module and can't import platform_mac without a cycle.
_BROWSER_BUNDLE_IDS = frozenset({
    "com.google.Chrome",
    "com.apple.Safari",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "company.thebrowser.Browser",
    "org.mozilla.firefox",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
})


def _compile(pattern: str) -> re.Pattern[str]:
    cached = _COMPILED_PATTERN_CACHE.get(pattern)
    if cached is not None:
        return cached
    compiled = re.compile(pattern)
    _COMPILED_PATTERN_CACHE[pattern] = compiled
    return compiled


def _collect_browser_titles(foreground: ForegroundInfo) -> list[str]:
    """Flatten every title-ish field on ForegroundInfo into a deduped list.
    Used by browser-spec matching so we can try regexes against every
    available title without worrying about which field the platform layer
    happened to populate.
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in (
        foreground.browser_tab_title,
        foreground.window_title,
        *foreground.browser_window_titles,
    ):
        if not t:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _collect_browser_urls(foreground: ForegroundInfo) -> list[str]:
    """Flatten the foreground tab URL + every background browser window
    URL into a deduped list. Used by browser-spec matching so url_patterns
    can hit either the foreground (user's active tab) or an Alt+Tab'd
    background browser window that's holding the mic.
    """
    out: list[str] = []
    seen: set[str] = set()
    for u in (
        foreground.browser_tab_url,
        *foreground.browser_window_urls,
    ):
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def match_whitelist(
    specs: list[DetectorSpec],
    foreground: ForegroundInfo,
    mic: MicState,
    *,
    exclude_app_keys: frozenset[str] = frozenset(),
) -> Optional[MatchResult]:
    """Return the first high-confidence match across ``specs``, or None.

    Precedence: desktop-app matches (``mic_session``) before browser-URL
    matches, so if Zoom desktop is running a meeting while Chrome has
    Google Meet open in the background, we attribute the match to Zoom.

    ``MatchResult.target_pids`` is populated directly from ``mic.holders``
    on both platforms now. The ArmController's
    ``resolve_pids_for_spec`` resolver is only consulted for the
    browser path when ``_pids_for_browser_holders`` returns empty (e.g.
    a macOS browser holder where the helper PID didn't survive the
    walk). Empty tuple ⇒ endpoint-wide capture.

    ``exclude_app_keys`` lets the caller skip specs that are currently
    suppressed (release pending after a decline or session end).
    Without this, a declined ``gmeet`` match in a background browser
    window would mask a ``chatgpt-com`` match in the foreground tab —
    the watcher only looks at the first match per poll, so the next
    valid match never gets a chance to fire its consent toast.
    """
    # User-disabled detectors are invisible to matching. Filter once here
    # so the three passes below don't each repeat the check. Same goes
    # for caller-supplied suppressions (declined / cooled-down apps).
    active_specs = [
        s for s in specs
        if not s.disabled and s.app_key not in exclude_app_keys
    ]

    holder_names = {h.process_name.lower() for h in mic.holders}
    holder_bundles = {h.bundle_id.lower() for h in mic.holders if h.bundle_id}

    # Pass 1 — desktop apps via direct mic-session hit. Works on both
    # platforms now: Windows from pycaw process names, macOS from the
    # audio-detect Swift helper (bundle ids).
    #
    # Two match flavors per spec:
    #   - Direct: spec.process_names ∩ holder_names, or spec.bundle_ids
    #     ∩ holder_bundles. The fast path.
    #   - Helper-bundle prefix: a holder's bundle id starts with
    #     "<spec.bundle_ids[i]>." (with the dot suffix to prevent false
    #     positives like "com.foo.app" matching "com.foo.app2"). Catches
    #     every Electron-based meeting app universally — Discord, Slack,
    #     Teams desktop, Signal, Skype, etc. — without needing per-app
    #     helper bundle id lists. Pure backstop: macOS's responsibility
    #     SPI already attributes most helpers to their main app upstream
    #     in get_mic_holders, but this catches the cases where it didn't.
    for spec in active_specs:
        if spec.is_browser:
            continue
        spec_names_lower = {p.lower() for p in spec.process_names}
        spec_bundles_lower = {b.lower() for b in spec.bundle_ids}

        if spec_names_lower & holder_names:
            return MatchResult(
                app_key=spec.app_key,
                display_name=spec.display_name,
                source="mic_session",
                target_pids=_pids_for_desktop_holders(spec, mic),
            )
        if spec_bundles_lower & holder_bundles:
            return MatchResult(
                app_key=spec.app_key,
                display_name=spec.display_name,
                source="mic_session",
                target_pids=_pids_for_desktop_holders(spec, mic),
            )
        # Helper-bundle prefix backstop.
        for spec_bundle in spec_bundles_lower:
            if any(b.startswith(spec_bundle + ".") for b in holder_bundles):
                return MatchResult(
                    app_key=spec.app_key,
                    display_name=spec.display_name,
                    source="mic_session",
                    target_pids=_pids_for_desktop_holders(spec, mic),
                )

    # Pass 3 — browsers. Gate on a browser actually holding the mic
    # (Windows: pycaw attribution; macOS: mic.active + browser-is-foreground).
    # We no longer require the browser to be foreground on Windows — the user
    # can Alt+Tab to a terminal during a Meet call and we still want to
    # attribute the mic-hold to the right browser spec.
    if _browser_holds_mic(mic, foreground):
        # Pass 3a — when the user is looking at a browser tab, that
        # foreground tab is ground truth for "what they're using right
        # now." Match every browser spec against the foreground-tab
        # URL/title alone first, so a chatgpt-com voice mode in the
        # foreground wins over a gmeet tab still open in another
        # window. Without this, detector list order decided (default
        # specs first, custom appended), and a background gmeet URL
        # could mask a foreground chatgpt-com match (user report
        # 2026-04-29).
        fg_url = foreground.browser_tab_url
        fg_title = foreground.browser_tab_title or foreground.window_title
        if fg_url:
            fg_urls = [fg_url]
            fg_titles = [fg_title] if fg_title else []
            for spec in active_specs:
                if not spec.is_browser:
                    continue
                if _browser_spec_matches(spec, fg_urls, fg_titles):
                    return MatchResult(
                        app_key=spec.app_key,
                        display_name=spec.display_name,
                        source="browser_mic_plus_url",
                        target_pids=_pids_for_browser_holders(mic),
                    )

        # Pass 3b — fallback. Foreground isn't a browser (Alt+Tabbed
        # to a terminal) or its URL/title didn't match anything we
        # know. Match against every visible browser window's URL +
        # title — preserves the v2.1.7 "Alt+Tab away from the browser
        # still attributes the mic-hold correctly" behavior.
        urls = _collect_browser_urls(foreground)
        titles = _collect_browser_titles(foreground)
        for spec in active_specs:
            if not spec.is_browser:
                continue
            if _browser_spec_matches(spec, urls, titles):
                return MatchResult(
                    app_key=spec.app_key,
                    display_name=spec.display_name,
                    source="browser_mic_plus_url",
                    target_pids=_pids_for_browser_holders(mic),
                )

    return None


def _pids_for_desktop_holders(spec: DetectorSpec, mic: MicState) -> tuple[int, ...]:
    """PIDs from ``mic.holders`` whose process name OR bundle id matches ``spec``.

    Both platforms now populate ``mic.holders`` with real PIDs (Windows:
    from pycaw, macOS: from the responsibility SPI). System-audio
    capture scopes to exactly these processes. Includes helper-bundle
    prefix matching so Discord's ``com.hnc.Discord.helper.Renderer``
    contributes its PID to a Discord arm even if get_mic_holders didn't
    already collapse it to the main bundle.
    """
    names = {p.lower() for p in spec.process_names}
    bundles = {b.lower() for b in spec.bundle_ids}
    pids: set[int] = set()
    for h in mic.holders:
        if h.pid <= 0:
            continue
        if h.process_name.lower() in names:
            pids.add(h.pid)
            continue
        if h.bundle_id and h.bundle_id.lower() in bundles:
            pids.add(h.pid)
            continue
        if h.bundle_id:
            hb = h.bundle_id.lower()
            if any(hb.startswith(sb + ".") for sb in bundles):
                pids.add(h.pid)
    return tuple(sorted(pids))


def _pids_for_browser_holders(mic: MicState) -> tuple[int, ...]:
    """PIDs from ``mic.holders`` that belong to any known browser process.

    Used by the browser match path. Per-tab scoping isn't possible (all tabs
    share the browser's PID tree) — documented as a known limitation.
    """
    pids = {
        h.pid for h in mic.holders
        if h.pid > 0 and h.process_name.lower() in BROWSER_PROCESS_NAMES
    }
    return tuple(sorted(pids))


def _browser_spec_matches(
    spec: DetectorSpec, urls: list[str], titles: list[str],
) -> bool:
    """True if ``spec`` matches any of ``urls`` (preferred) or ``titles``.

    URL patterns are tried against ``urls`` first — that's the reliable
    signal when UIA / AppleScript can read the active tab. They're also
    tried against ``titles`` as a legacy fallback for the tiny minority
    of configs that put the URL in the title. ``title_patterns`` are
    title-only any-of regexes — that's how Windows matches the ship-with
    Meet/Zoom-web specs when UIA can't read the omnibox.
    """
    for pattern in spec.url_patterns:
        rx = _compile(pattern)
        for u in urls:
            if rx.search(u):
                return True
        for t in titles:
            if rx.search(t):
                return True
    for pattern in spec.title_patterns:
        rx = _compile(pattern)
        for t in titles:
            if rx.search(t):
                return True
    return False


def arm_app_still_holding_mic(
    app_key: str,
    specs: list[DetectorSpec],
    mic: MicState,
    foreground: ForegroundInfo,
    *,
    arm_pids_alive: Optional[bool] = None,
) -> bool:
    """For the meeting-ended watcher: is the arm-app still a mic-holder?

    Returns True if the spec with ``app_key`` is still detectable via
    the same signal type we'd use to match it fresh. Used per-poll by
    the watcher; a streak of ``False`` longer than the grace window
    triggers the meeting-ended toast.

    Browser specs are intentionally evaluated only at the browser-
    process level here — we do NOT re-check URL or title. AX (and
    Windows UIA) typically only see the focused tab; tabbing away from
    chatgpt-com to read email would otherwise flip the title list and
    trip a false meeting-ended toast. The mic-release signal is ground
    truth: hanging up a call drops the browser's capture session,
    removing it from ``mic.holders``.

    ``arm_pids_alive`` is now ignored — ``mic.holders`` is reliable on
    both platforms (Windows: pycaw; macOS v2.5+: audio-detect helper),
    so the old "PIDs are alive" tiebreaker isn't needed. The
    ``whitelist_arm_release_grace_secs`` window in the controller
    absorbs single-poll transients (failed helper invocation, etc.).
    Parameter retained for ABI compatibility with existing callers.
    """
    spec = next((s for s in specs if s.app_key == app_key), None)
    if spec is None:
        return False

    holder_names = {h.process_name.lower() for h in mic.holders}
    holder_bundles = {h.bundle_id.lower() for h in mic.holders if h.bundle_id}

    if not spec.is_browser:
        spec_names = {p.lower() for p in spec.process_names}
        spec_bundles = {b.lower() for b in spec.bundle_ids}
        if spec_names & holder_names:
            return True
        if spec_bundles & holder_bundles:
            return True
        # Helper-bundle prefix backstop (Electron apps).
        for sb in spec_bundles:
            if any(b.startswith(sb + ".") for b in holder_bundles):
                return True
        return False

    # Browser spec — direct mic-holder check. Same path on both platforms.
    return _browser_holds_mic(mic, foreground)
