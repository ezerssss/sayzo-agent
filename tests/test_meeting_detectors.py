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


# ---- user-added URL detectors (Settings → Web tab) ---------------------
#
# These mirror the spec shape produced by ``_submit_web`` in
# ``gui/settings_window.py``: ``is_browser=True`` + ``url_patterns`` only,
# NO ``title_patterns``. Before v1.5.0 the foreground tab URL was always
# ``None`` on Windows (no UIA read), so these specs could never match and
# custom URLs pasted in Settings silently did nothing. With
# ``platform_win.get_browser_tab_url`` populating ``browser_tab_url`` +
# ``browser_window_urls``, the matcher should now find them.


def _custom_url_specs(pattern: str) -> list[DetectorSpec]:
    return [
        DetectorSpec(
            app_key="custom_site",
            display_name="Custom Site",
            is_browser=True,
            url_patterns=[pattern],
        ),
    ]


def test_custom_url_spec_matches_via_foreground_tab_url():
    """The ``_submit_web`` regex ``^https://chatgpt\\.com/`` matches when
    the foreground browser's active tab URL is populated."""
    specs = _custom_url_specs(r"^https://chatgpt\.com/")
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://chatgpt.com/c/abc-123",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])
    r = match_whitelist(specs, fg, mic)
    assert r is not None
    assert r.app_key == "custom_site"
    assert r.source == "browser_mic_plus_url"


def test_custom_url_spec_matches_via_background_browser_window_url():
    """User Alt+Tab'd away from the browser during a voice chat. Chrome
    still holds the mic, and one of the visible Chrome windows has the
    matching URL. The matcher should walk ``browser_window_urls``."""
    specs = _custom_url_specs(r"^https://chatgpt\.com/")
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_urls=(
            "https://news.ycombinator.com/",
            "https://chatgpt.com/c/xyz",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])
    r = match_whitelist(specs, fg, mic)
    assert r is not None
    assert r.app_key == "custom_site"
    assert r.source == "browser_mic_plus_url"


def test_custom_url_spec_no_match_when_urls_and_titles_absent():
    """Pre-v1.5 behavior under a UIA failure: no URL read, no title
    pattern. The custom spec can't match — caller should surface the
    unmatched mic-holder in seen-apps instead of firing a wrong toast.
    """
    specs = _custom_url_specs(r"^https://chatgpt\.com/")
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url=None,
        window_title="ChatGPT - Google Chrome",  # title has no URL — can't match
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])
    assert match_whitelist(specs, fg, mic) is None


def test_custom_strict_url_spec_matches_exact_meeting_only():
    """``_submit_web`` strict mode bakes the path into the regex (e.g.
    ``^https://example\\.com/room/42$``). The matcher should match only
    that exact URL — navigating to a different room breaks the match."""
    specs = _custom_url_specs(r"^https://example\.com/room/42")
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])

    fg_correct = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://example.com/room/42",
    )
    r = match_whitelist(specs, fg_correct, mic)
    assert r is not None and r.app_key == "custom_site"

    fg_other_room = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://example.com/room/99",
    )
    assert match_whitelist(specs, fg_other_room, mic) is None


# ---- target_pids population --------------------------------------------
#
# Per-app system-audio capture (v1.7.0) scopes the system loopback to the
# PIDs that matched the whitelist. On Windows those PIDs come from
# ``mic.holders`` inline; on macOS the ArmController resolves them via a
# platform helper (psutil + NSWorkspace).


def test_zoom_match_carries_holder_pid():
    """Windows desktop match: ``target_pids`` = Zoom's PID from mic.holders."""
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.target_pids == (1234,)


def test_zoom_match_carries_all_matching_holder_pids():
    """Multiple Zoom processes (main + CptHost) → all PIDs."""
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[
        MicHolder("zoom.exe", 1234),
        MicHolder("CptHost.exe", 4321),
        MicHolder("unrelated.exe", 9999),  # shouldn't appear
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.target_pids == (1234, 4321)


def test_gmeet_match_carries_browser_holder_pid():
    """Browser match: ``target_pids`` = PIDs of browser process(es) holding
    the mic. Per-tab scoping isn't possible (known limitation)."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://meet.google.com/abc-defg-hij",
    )
    mic = MicState(holders=[
        MicHolder("chrome.exe", 1111),
        MicHolder("spotify.exe", 2222),  # not a browser, must not be included
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"
    assert r.target_pids == (1111,)


def test_macos_proxy_match_has_empty_target_pids():
    """macOS proxy path can't attribute PIDs inline (``mic.holders=[]``);
    the ArmController resolves them via a platform helper before arming.
    The MatchResult itself comes back with an empty tuple."""
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState(
        active=True,
        running_processes=frozenset({"us.zoom.xos"}),
    )
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.source == "mic_active_plus_running"
    assert r.target_pids == ()


def test_custom_url_spec_arm_app_still_holding_via_window_urls():
    """Meeting-ended watcher: a custom URL spec should stay ``True`` as
    long as a browser still has the mic AND any browser window URL still
    matches the spec. Mirrors the fg-away case above."""
    specs = _custom_url_specs(r"^https://chatgpt\.com/")
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_urls=("https://chatgpt.com/c/xyz",),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])
    assert arm_app_still_holding_mic("custom_site", specs, mic, fg) is True

    # And goes False the moment the tab navigates away.
    fg_elsewhere = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_urls=("https://news.ycombinator.com/",),
    )
    assert arm_app_still_holding_mic(
        "custom_site", specs, mic, fg_elsewhere,
    ) is False
