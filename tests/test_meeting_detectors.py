"""Tests for the pure meeting-detection matching logic.

All tests drive ``sayzo_agent.arm.detectors`` with synthetic ``ForegroundInfo``
and ``MicState`` inputs. No OS calls, no real pycaw / CoreAudio.
"""
from __future__ import annotations

import pytest

from sayzo_agent.arm.detectors import (
    ForegroundInfo,
    MatchResult,
    MicHolder,
    MicState,
    arm_app_still_holding_mic,
    match_whitelist,
)
from sayzo_agent.config import DetectorSpec, default_detector_specs


SPECS = default_detector_specs()


def _specs_with(app_key: str, **overrides) -> list[DetectorSpec]:
    """Return a defaults list where the spec with ``app_key`` has been patched."""
    out: list[DetectorSpec] = []
    for s in default_detector_specs():
        if s.app_key == app_key:
            patched = s.model_copy(update=overrides)
            out.append(patched)
        else:
            out.append(s)
    return out


# ---- Zoom ---------------------------------------------------------------


def test_zoom_launcher_no_mic_session_no_match():
    """Zoom open but not in a meeting holds no mic session → no match."""
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState()  # nothing holding the mic
    assert match_whitelist(SPECS, fg, mic) is None


def test_zoom_meeting_holds_mic_matches():
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"
    assert r.source == "mic_session"


def test_zoom_match_independent_of_foreground():
    """Zoom can be backgrounded during a presentation; the match should
    still fire because the mic signal doesn't depend on foreground."""
    fg = ForegroundInfo(process_name="POWERPNT.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"


# ---- Discord (the user's specific concern) -----------------------------


def test_discord_in_voice_call_matches_without_title_regex():
    """Discord never changes its window title during a voice call; we must
    still match when it's holding the mic."""
    fg = ForegroundInfo(process_name="Discord.exe", window_title="Discord")
    mic = MicState(holders=[MicHolder("Discord.exe", 5555)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "discord"


def test_discord_browsing_text_channels_no_match():
    fg = ForegroundInfo(process_name="Discord.exe")
    mic = MicState()  # no mic session
    assert match_whitelist(SPECS, fg, mic) is None


# ---- Google Meet (browser + URL) ---------------------------------------


def test_gmeet_landing_page_no_match():
    """Root page: browser might have mic (e.g. for mic-test pre-join) but
    URL doesn't match the meeting-code pattern → no toast."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://meet.google.com/",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert match_whitelist(SPECS, fg, mic) is None


def test_gmeet_in_call_matches():
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://meet.google.com/abc-defg-hij",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"
    assert r.source == "browser_mic_plus_url"


def test_gmeet_no_match_when_browser_not_foreground():
    """Browser matches require the browser to be foreground (so we know WHICH
    tab URL to read). If user is in Zoom desktop AND a Gmeet tab is open in
    a background Chrome, we attribute to Zoom, not Gmeet."""
    fg = ForegroundInfo(process_name="zoom.exe")  # not a browser
    mic = MicState(holders=[
        MicHolder("chrome.exe", 1111),
        MicHolder("zoom.exe", 2222),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"


def test_gmeet_title_fallback_matches_without_url():
    """macOS without Automation permission: we have tab title but no URL.
    The matcher should fall back to regex against the title."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url=None,
        browser_tab_title="https://meet.google.com/abc-defg-hij",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"


def test_gmeet_matches_windows_chrome_title_when_browser_foreground():
    """Windows: no tab URL, Chrome is foreground, window title has the
    canonical Meet format. The new title_patterns should match."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url=None,
        window_title="Meet - ojg-gdmq-tzn - Google Chrome",
        browser_tab_title="Meet - ojg-gdmq-tzn - Google Chrome",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"
    assert r.source == "browser_mic_plus_url"


def test_gmeet_matches_via_background_browser_window_on_windows():
    """Windows: user Alt+Tab'd to a terminal while in Meet. Chrome isn't
    foreground, but it still holds the mic and one of the browser's visible
    windows has the Meet title. The matcher should walk
    browser_window_titles and match anyway."""
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_titles=(
            "(1) YouTube - Google Chrome",
            "Meet - ojg-gdmq-tzn - Google Chrome",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"
    assert r.source == "browser_mic_plus_url"


def test_gmeet_no_match_when_no_browser_window_has_meet():
    """Chrome holds the mic but none of its windows is a Meet URL/title
    (e.g. the user is on a WebRTC demo site). No match — we shouldn't
    blindly attribute the capture to gmeet."""
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_titles=(
            "YouTube - Google Chrome",
            "GitHub · Where software is built - Google Chrome",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert match_whitelist(SPECS, fg, mic) is None


def test_gmeet_title_prefix_notification_count_still_matches():
    """Chrome prefixes window titles with ``(N) `` when the tab has unread
    notifications. The title regex has to tolerate that."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url=None,
        window_title="(3) Meet - ojg-gdmq-tzn - Google Chrome",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"


# ---- Teams web ---------------------------------------------------------


def test_teams_web_in_meetup_join_url_matches():
    fg = ForegroundInfo(
        process_name="msedge.exe",
        is_browser=True,
        browser_tab_url="https://teams.microsoft.com/dl/launcher/l/meetup-join/19:abc",
    )
    mic = MicState(holders=[MicHolder("msedge.exe", 3333)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "teams_web"


# ---- macOS proxy path --------------------------------------------------


def test_macos_proxy_matches_when_mic_active_and_process_running_and_foreground():
    """macOS can't attribute mic to a specific PID cheaply. The proxy is:
    mic is active system-wide AND a whitelisted process is running AND is
    currently frontmost."""
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState(
        active=True,
        running_processes=frozenset({"us.zoom.xos"}),
    )
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"
    assert r.source == "mic_active_plus_running"


def test_macos_proxy_no_match_when_mic_inactive():
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState(active=False, running_processes=frozenset({"us.zoom.xos"}))
    assert match_whitelist(SPECS, fg, mic) is None


def test_macos_proxy_no_match_when_whitelisted_app_not_frontmost():
    """Whitelisted app is running and mic is active, but the user is focused
    on something else — could be a browser with Gmeet (handled elsewhere)
    or something unrelated. Only the direct-frontmost check should match."""
    fg = ForegroundInfo(bundle_id="com.apple.TextEdit")
    mic = MicState(
        active=True,
        running_processes=frozenset({"us.zoom.xos"}),
    )
    assert match_whitelist(SPECS, fg, mic) is None


# ---- arm_app_still_holding_mic -----------------------------------------


def test_arm_app_still_holding_after_match():
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    fg = ForegroundInfo(process_name="zoom.exe")
    assert arm_app_still_holding_mic("zoom", SPECS, mic, fg) is True


def test_arm_app_no_longer_holding_after_meeting_ends():
    mic = MicState()
    fg = ForegroundInfo(process_name="zoom.exe")
    assert arm_app_still_holding_mic("zoom", SPECS, mic, fg) is False


def test_arm_app_unknown_key_returns_false():
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    fg = ForegroundInfo(process_name="zoom.exe")
    assert arm_app_still_holding_mic("unknown_app", SPECS, mic, fg) is False


def test_arm_app_browser_still_holding_with_url_match():
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://meet.google.com/abc-defg-hij",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert arm_app_still_holding_mic("gmeet", SPECS, mic, fg) is True


def test_arm_app_browser_released_when_tab_navigated_away():
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://news.ycombinator.com/",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert arm_app_still_holding_mic("gmeet", SPECS, mic, fg) is False


# ---- disabled flag -----------------------------------------------------


def test_disabled_desktop_spec_does_not_match():
    """A spec with ``disabled=True`` is invisible to ``match_whitelist`` —
    toggling an app off in Settings suppresses future consent toasts
    without losing the spec's process names / URL patterns."""
    specs = _specs_with("zoom", disabled=True)
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    assert match_whitelist(specs, fg, mic) is None


def test_disabled_browser_spec_does_not_match():
    specs = _specs_with("gmeet", disabled=True)
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://meet.google.com/abc-defg-hij",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert match_whitelist(specs, fg, mic) is None


def test_disabled_spec_does_not_affect_other_apps():
    """Disabling Zoom doesn't stop a Discord voice call from matching."""
    specs = _specs_with("zoom", disabled=True)
    fg = ForegroundInfo(process_name="Discord.exe")
    mic = MicState(holders=[MicHolder("Discord.exe", 5555)])
    r = match_whitelist(specs, fg, mic)
    assert r is not None
    assert r.app_key == "discord"


def test_arm_app_still_holding_ignores_disabled_flag():
    """Session-lifecycle check is *not* affected by the disabled flag: if
    the user disables Zoom mid-session, the current session should keep
    going, not get cut short by the meeting-ended watcher."""
    specs = _specs_with("zoom", disabled=True)
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    assert arm_app_still_holding_mic("zoom", specs, mic, fg) is True
