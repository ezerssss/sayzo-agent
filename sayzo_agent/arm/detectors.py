"""Pure matching logic for the meeting-app whitelist.

The detector inputs come from platform-specific queries (see
``platform_win.py`` / ``platform_mac.py``): a list of processes currently
holding the default microphone as an active capture session (Windows),
or a boolean "is any process capturing from the mic" + list of running
whitelisted processes (macOS), plus the frontmost app + (on macOS) the
active browser tab URL.

This module is pure — no OS calls, no imports beyond the standard library
and config types. Tests drive it with synthetic ``ForegroundInfo`` /
``MicState`` inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from ..config import DetectorSpec


MatchSource = Literal[
    # Windows — we saw a whitelisted process as a mic-session holder directly.
    "mic_session",
    # macOS — mic is active system-wide AND a whitelisted process is running
    # AND was frontmost within the configured window.
    "mic_active_plus_running",
    # Either platform — browser holds the mic AND the active tab URL matches
    # a DetectorSpec with is_browser=True.
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


@dataclass(frozen=True)
class MicHolder:
    """One process currently holding an active capture session on the default
    microphone. Populated on Windows by ``get_mic_holders()``; on macOS the
    equivalent is a system-wide "mic-active" bit + psutil running-process
    list (see ``platform_mac.py``)."""

    process_name: str
    pid: int = -1


@dataclass(frozen=True)
class MicState:
    """Normalized mic-holder snapshot that works on both platforms.

    Windows: ``holders`` comes directly from WASAPI session enumeration.
    macOS: ``active`` is set when ``kAudioDevicePropertyDeviceIsRunningSomewhere``
    is True; ``running_processes`` lists psutil-visible process names. The
    matcher treats ``active and process_name in running_processes`` as
    equivalent to a direct mic-holder match under the
    ``mic_active_plus_running`` source.
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
    """

    app_key: str
    display_name: str
    source: MatchSource


_COMPILED_URL_CACHE: dict[str, re.Pattern[str]] = {}


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

    Windows: direct check against ``mic.holders``. macOS has no per-process
    mic attribution; treat ``mic.active`` + a browser being frontmost as
    the equivalent signal.
    """
    for h in mic.holders:
        if h.process_name.lower() in BROWSER_PROCESS_NAMES:
            return True
    return mic.active and foreground.is_browser


def _compile(pattern: str) -> re.Pattern[str]:
    cached = _COMPILED_URL_CACHE.get(pattern)
    if cached is not None:
        return cached
    compiled = re.compile(pattern)
    _COMPILED_URL_CACHE[pattern] = compiled
    return compiled


def match_whitelist(
    specs: list[DetectorSpec],
    foreground: ForegroundInfo,
    mic: MicState,
) -> Optional[MatchResult]:
    """Return the first high-confidence match across ``specs``, or None.

    Precedence: desktop-app matches (mic_session / mic_active_plus_running)
    before browser-URL matches, so if Zoom desktop is running a meeting
    while Chrome has Google Meet open in the background, we attribute the
    match to Zoom.
    """
    holder_names = {h.process_name.lower() for h in mic.holders}

    # Pass 1 — desktop apps via direct mic-session hit (Windows).
    for spec in specs:
        if spec.is_browser:
            continue
        if not spec.process_names:
            continue
        for proc in spec.process_names:
            if proc.lower() in holder_names:
                return MatchResult(
                    app_key=spec.app_key,
                    display_name=spec.display_name,
                    source="mic_session",
                )

    # Pass 2 — macOS proxy. Mic is active system-wide AND a whitelisted
    # process is running. This catches Discord voice calls / Zoom meetings
    # on macOS where we can't attribute mic-use per-process cheaply.
    if mic.active and mic.running_processes:
        fg_proc = (foreground.process_name or "").lower()
        fg_bundle = (foreground.bundle_id or "").lower()
        running_lower = {p.lower() for p in mic.running_processes}
        for spec in specs:
            if spec.is_browser:
                continue
            targets: list[str] = []
            targets.extend(p.lower() for p in spec.process_names)
            targets.extend(b.lower() for b in spec.bundle_ids)
            if not targets:
                continue
            if not any(t in running_lower or t == fg_proc or t == fg_bundle
                       for t in targets):
                continue
            # Require the matched app to currently be frontmost (v1 — the
            # foreground cache is owned by the caller, so we just check the
            # current snapshot here).
            if fg_proc and any(t == fg_proc for t in targets):
                return MatchResult(
                    app_key=spec.app_key,
                    display_name=spec.display_name,
                    source="mic_active_plus_running",
                )
            if fg_bundle and any(t == fg_bundle for t in targets):
                return MatchResult(
                    app_key=spec.app_key,
                    display_name=spec.display_name,
                    source="mic_active_plus_running",
                )

    # Pass 3 — browsers. Need the browser to be the mic-holder (Windows) or
    # the foreground app (macOS — since we can't attribute per-process) AND
    # the active tab URL to match.
    if foreground.is_browser:
        url = foreground.browser_tab_url or ""
        # On macOS without Automation permission we get only the title; try
        # a loose fallback against that (the user-facing regexes include
        # a "Meet – " style pattern for Google Meet).
        title = foreground.browser_tab_title or foreground.window_title or ""
        if _browser_holds_mic(mic, foreground):
            for spec in specs:
                if not spec.is_browser:
                    continue
                for pattern in spec.url_patterns:
                    rx = _compile(pattern)
                    if rx.search(url) or rx.search(title):
                        return MatchResult(
                            app_key=spec.app_key,
                            display_name=spec.display_name,
                            source="browser_mic_plus_url",
                        )

    return None


def arm_app_still_holding_mic(
    app_key: str,
    specs: list[DetectorSpec],
    mic: MicState,
    foreground: ForegroundInfo,
) -> bool:
    """For the meeting-ended watcher: is the arm-app still a mic-holder?

    Returns True if the spec with ``app_key`` is still detectable via the
    same signal type we'd use to match it fresh. Used per-poll by the
    watcher; a streak of ``False`` longer than the grace window triggers
    the meeting-ended toast.
    """
    spec = next((s for s in specs if s.app_key == app_key), None)
    if spec is None:
        return False

    holder_names = {h.process_name.lower() for h in mic.holders}

    if not spec.is_browser:
        for proc in spec.process_names:
            if proc.lower() in holder_names:
                return True
        if mic.active:
            running_lower = {p.lower() for p in mic.running_processes}
            fg_proc = (foreground.process_name or "").lower()
            fg_bundle = (foreground.bundle_id or "").lower()
            for target in [*spec.process_names, *spec.bundle_ids]:
                t = target.lower()
                if t in running_lower or t == fg_proc or t == fg_bundle:
                    return True
        return False

    # Browser spec: still-holding means the currently-focused browser has
    # the mic AND the active tab URL still matches one of the patterns.
    if not foreground.is_browser:
        return False
    if not _browser_holds_mic(mic, foreground):
        return False
    url = foreground.browser_tab_url or ""
    title = foreground.browser_tab_title or foreground.window_title or ""
    for pattern in spec.url_patterns:
        rx = _compile(pattern)
        if rx.search(url) or rx.search(title):
            return True
    return False
