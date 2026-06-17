"""Tests for the pure meeting-detection matching logic.

All tests drive ``sayzo_agent.arm.detectors`` with synthetic ``ForegroundInfo``
and ``MicState`` inputs. No OS calls, no real pycaw / CoreAudio.
"""
from __future__ import annotations


from sayzo_agent.arm.detectors import (
    ForegroundInfo,
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


def test_non_browser_foreground_window_title_does_not_leak_into_browser_match():
    """Regression for 2026-05-07 user report: Discord desktop (a non-browser
    app) is foreground while chrome holds the mic for an unrelated capture.
    Discord desktop's window title (``"#channel | Server - Discord"``) used
    to be unconditionally pulled into the browser-spec title pool by
    ``_collect_browser_titles``, where it hit ``discord_web``'s lax
    ``(?i)\\bDiscord\\b`` regex and produced a false match. The matcher
    must ignore ``window_title`` from a non-browser foreground; only
    actual browser-owned titles (``browser_window_titles`` /
    ``browser_tab_title``) participate in browser-spec matching."""
    fg = ForegroundInfo(
        process_name="Discord.exe",
        is_browser=False,
        window_title="#development | Threadlify - Discord",
        browser_window_titles=(
            "Claude - Google Chrome",
            "Messenger | Facebook - Google Chrome",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 10792)])
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


# ---- macOS bundle-id matching (v2.5+ — Pass 1 now uses real holders) ---
#
# The pre-v2.5 macOS proxy (``mic_active_plus_running``) required the
# meeting app to be foreground, because we had no per-process mic
# attribution on Mac. v2.5 ships the audio-detect Swift helper which
# gives us direct holders with bundle ids — so Pass 1 now works on Mac
# the same way it works on Windows. These tests cover the new path.


def test_macos_zoom_match_via_bundle_id_holder():
    """macOS holders carry bundle ids (not exe names). Pass 1 matches
    ``us.zoom.xos`` against the zoom spec's bundle_ids."""
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState(holders=[
        MicHolder(process_name="us.zoom.xos", pid=1234, bundle_id="us.zoom.xos"),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"
    assert r.source == "mic_session"
    assert r.target_pids == (1234,)


def test_macos_match_independent_of_foreground():
    """v2.5: the foreground requirement is GONE. Zoom in a meeting +
    user Alt-tabbed to TextEdit should still match — same as Windows."""
    fg = ForegroundInfo(bundle_id="com.apple.TextEdit")
    mic = MicState(holders=[
        MicHolder(process_name="us.zoom.xos", pid=1234, bundle_id="us.zoom.xos"),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "zoom"


def test_macos_no_match_when_no_holders():
    """No mic activity → no match. Foreground irrelevant."""
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState()  # holders=[] regardless of running_processes
    assert match_whitelist(SPECS, fg, mic) is None


def test_macos_discord_helper_bundle_prefix_matches_discord():
    """Electron apps capture via ``<app-bundle>.helper.<role>`` processes.
    The matcher's helper-bundle prefix backstop catches them even if the
    SPI walk-up didn't already collapse them to the main bundle."""
    fg = ForegroundInfo(bundle_id="com.apple.TextEdit")
    mic = MicState(holders=[
        MicHolder(
            process_name="com.hnc.Discord.helper.Renderer",
            pid=5555,
            bundle_id="com.hnc.Discord.helper.Renderer",
        ),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "discord"
    assert r.source == "mic_session"


def test_macos_browser_match_via_chrome_bundle_holder():
    """Chrome capturing the mic on macOS shows up as a holder with
    bundle id ``com.google.Chrome`` (after the Swift helper resolves
    the audio helper to its responsible browser). Combined with a
    matching meeting URL/title in any browser window, this fires the
    browser-spec match — no browser-foreground requirement."""
    fg = ForegroundInfo(
        process_name="Notes",
        bundle_id="com.apple.TextEdit",
        is_browser=False,
        browser_window_titles=("Meet - abc-defg-hij - Google Chrome",),
    )
    mic = MicState(holders=[
        MicHolder(
            process_name="com.google.Chrome",
            pid=7777,
            bundle_id="com.google.Chrome",
        ),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"
    assert r.source == "browser_mic_plus_url"


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


def test_arm_app_browser_still_holding_when_tab_navigated_away():
    """v2.1.7+: switching tabs in the same browser window does NOT trip
    the meeting-ended check. UIAutomation typically only sees the
    focused tab's URL, so a user tabbing to email or notes during a
    chatgpt-com voice session would otherwise trigger a false toast.
    The arm session is bound to the browser PID at scope time; "still
    holding" reduces to "browser still holds the mic.\""""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://news.ycombinator.com/",
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1111)])
    assert arm_app_still_holding_mic("gmeet", SPECS, mic, fg) is True


def test_arm_app_browser_released_when_browser_drops_mic():
    """The actual ground truth — when the browser process leaves
    mic.holders entirely (user ended the voice session), still-holding
    returns False and the meeting-ended path can fire."""
    fg = ForegroundInfo(
        process_name="chrome.exe",
        is_browser=True,
        browser_tab_url="https://news.ycombinator.com/",
    )
    mic = MicState(holders=[])  # browser no longer holds the mic
    assert arm_app_still_holding_mic("gmeet", SPECS, mic, fg) is False


# ---- macOS PID-binding via mic.holders ---------------------------------


def test_arm_app_browser_macos_alt_tab_still_holding():
    """macOS v2.5+: mic.holders carries Chrome's bundle id whenever
    Chrome is capturing (resolved via the responsibility SPI). Alt-
    tabbing to Notes during a Meet call leaves holders unchanged; the
    meeting-ended watcher correctly sees we're still holding."""
    fg = ForegroundInfo(
        process_name="Notes",
        bundle_id="com.apple.TextEdit",
        is_browser=False,
    )
    mic = MicState(holders=[
        MicHolder(
            process_name="com.google.Chrome",
            pid=1111,
            bundle_id="com.google.Chrome",
        ),
    ])
    assert arm_app_still_holding_mic("gmeet", SPECS, mic, fg) is True


def test_arm_app_browser_macos_no_holder_returns_false():
    """When the meeting actually ends, the browser drops its capture
    session and disappears from mic.holders. That's the ground-truth
    signal — regardless of whether the browser process is still alive
    (user might leave Chrome open)."""
    fg = ForegroundInfo(
        process_name="Notes",
        bundle_id="com.apple.TextEdit",
        is_browser=False,
    )
    mic = MicState(holders=[])
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
# These mirror the spec shape the Settings Add-app dialog produces for the
# Web tab: ``is_browser=True`` + ``url_patterns`` only, NO
# ``title_patterns``. Before v1.5.0 the foreground tab URL was always
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


# ---- exclude_app_keys (decline-doesn't-mask, v1.8.2) -------------------


def test_exclude_app_keys_skips_first_match_finds_next():
    """Browser has both Meet and ChatGPT URLs visible. Excluding the
    site the user is actively in must let the OTHER match be found
    instead, so a declined site doesn't shadow a backgrounded meeting
    that's also a candidate.

    v2.1.10+: foreground tab URL gets priority via Pass 3a, so the
    no-exclude case picks the foreground site first. Exclusion still
    works correctly to fall through to background matches in Pass 3b."""
    custom = DetectorSpec(
        app_key="chatgpt-com", display_name="ChatGPT", is_browser=True,
        url_patterns=[r"^https://chatgpt\.com/"],
    )
    specs = default_detector_specs() + [custom]
    fg = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://chatgpt.com/c/abc",
        browser_window_urls=(
            "https://chatgpt.com/c/abc",
            "https://meet.google.com/aaa-bbbb-ccc",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1234)])

    # Without exclusion: chatgpt-com (foreground tab) wins via Pass 3a.
    r = match_whitelist(specs, fg, mic)
    assert r is not None and r.app_key == "chatgpt-com"

    # With gmeet excluded: chatgpt-com still wins (Pass 3a, gmeet
    # never reached anyway).
    r = match_whitelist(specs, fg, mic, exclude_app_keys=frozenset({"gmeet"}))
    assert r is not None and r.app_key == "chatgpt-com"

    # With chatgpt-com excluded: gmeet wins via Pass 3b fallback
    # against the background browser window URL.
    r = match_whitelist(specs, fg, mic, exclude_app_keys=frozenset({"chatgpt-com"}))
    assert r is not None and r.app_key == "gmeet"


def test_pass3_prefers_foreground_tab_over_background_window():
    """When user is on chatgpt foreground while gmeet is still active
    in a background window, the toast must fire for chatgpt-com (the
    user's actual focus), not gmeet (which happens to come first in
    detector order). Pre-v2.1.10, detector list order decided and
    users hit "Recording Google Meet?" when they meant to capture
    chatgpt voice mode (user report 2026-04-29)."""
    custom = DetectorSpec(
        app_key="chatgpt-com", display_name="ChatGPT", is_browser=True,
        url_patterns=[r"^https://chatgpt\.com/"],
    )
    specs = default_detector_specs() + [custom]
    fg = ForegroundInfo(
        process_name="chrome.exe", is_browser=True,
        browser_tab_url="https://chatgpt.com/c/abc",
        browser_tab_title="ChatGPT - Voice mode",
        window_title="ChatGPT - Voice mode",
        browser_window_urls=(
            "https://chatgpt.com/c/abc",
            "https://meet.google.com/aaa-bbbb-ccc",
        ),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1234)])
    r = match_whitelist(specs, fg, mic)
    assert r is not None
    assert r.app_key == "chatgpt-com"


def test_pass3_falls_back_when_browser_not_foreground():
    """When the user has Alt+Tabbed away from the browser entirely
    (foreground.is_browser is False so foreground.browser_tab_url is
    None), Pass 3a is skipped and Pass 3b matches against every
    visible browser window's URL — preserving the v2.1.7 behavior
    where a Meet call in a background browser still fires its toast."""
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_tab_url=None,  # not a browser foreground
        browser_window_urls=("https://meet.google.com/aaa-bbbb-ccc",),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 1234)])
    r = match_whitelist(default_detector_specs(), fg, mic)
    assert r is not None
    assert r.app_key == "gmeet"


def test_exclude_app_keys_returns_none_when_only_match_is_excluded():
    fg = ForegroundInfo(process_name="zoom.exe")
    mic = MicState(holders=[MicHolder("zoom.exe", 1234)])
    r = match_whitelist(SPECS, fg, mic, exclude_app_keys=frozenset({"zoom"}))
    assert r is None


def test_macos_match_has_target_pids_from_holders():
    """v2.5: macOS holders carry real PIDs (resolved via the
    responsibility SPI), so MatchResult.target_pids comes back populated
    inline — same as Windows. The ArmController no longer needs to
    re-resolve via a platform helper for the desktop case."""
    fg = ForegroundInfo(bundle_id="us.zoom.xos")
    mic = MicState(holders=[
        MicHolder(process_name="us.zoom.xos", pid=4242, bundle_id="us.zoom.xos"),
    ])
    r = match_whitelist(SPECS, fg, mic)
    assert r is not None
    assert r.source == "mic_session"
    assert r.target_pids == (4242,)


def test_custom_url_spec_arm_app_still_holding_via_browser_pid():
    """v2.1.7+: a custom URL spec stays ``True`` as long as a browser
    still has the mic, regardless of the URL list. The URL re-check was
    removed because UIAutomation typically only surfaces the focused
    tab — switching tabs to take notes during the session would
    otherwise trip a false meeting-ended toast."""
    specs = _custom_url_specs(r"^https://chatgpt\.com/")
    fg = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_urls=("https://chatgpt.com/c/xyz",),
    )
    mic = MicState(holders=[MicHolder("chrome.exe", 9999)])
    assert arm_app_still_holding_mic("custom_site", specs, mic, fg) is True

    # User tabs to a non-matching URL — still True, because the browser
    # PID still holds the mic. This is the new behavior; the old build
    # returned False here and fired a spurious meeting-ended toast.
    fg_elsewhere = ForegroundInfo(
        process_name="WindowsTerminal.exe",
        is_browser=False,
        browser_window_urls=("https://news.ycombinator.com/",),
    )
    assert arm_app_still_holding_mic(
        "custom_site", specs, mic, fg_elsewhere,
    ) is True

    # Only when the browser drops the mic entirely does still-holding
    # return False.
    mic_no_browser = MicState(holders=[])
    assert arm_app_still_holding_mic(
        "custom_site", specs, mic_no_browser, fg_elsewhere,
    ) is False
