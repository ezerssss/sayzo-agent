"""Tests for the URL + key helpers that drive the Meeting Apps settings
pane's Add-app dialog.

These are pure functions — no tkinter required for the test to run.
"""
from __future__ import annotations

import re

import pytest

from sayzo_agent.gui.settings_window import (
    _display_name_from_host,
    _friendly_url_pattern,
    _parse_meeting_url,
    _unique_app_key,
    _url_pattern,
)


# ---- _parse_meeting_url ------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://meet.google.com/abc-defg-hij", ("meet.google.com", "/abc-defg-hij")),
    ("meet.google.com/xyz-pqrs-tuv", ("meet.google.com", "/xyz-pqrs-tuv")),
    ("https://zoom.us/j/1234567890", ("zoom.us", "/j/1234567890")),
    (
        "https://teams.microsoft.com/l/meetup-join/abc",
        ("teams.microsoft.com", "/l/meetup-join/abc"),
    ),
    # Trailing slash is stripped.
    ("https://meet.google.com/abc-def-ghi/", ("meet.google.com", "/abc-def-ghi")),
    # Query strings are discarded (regex is built from path only).
    (
        "https://zoom.us/j/1234567890?pwd=xyz",
        ("zoom.us", "/j/1234567890"),
    ),
    # Bare domains: host-only input is accepted with an empty path. Non-strict
    # ``_url_pattern`` discards the path anyway, so the whole-host match works;
    # ``_submit_web`` is responsible for rejecting strict+empty-path at submit.
    ("chatgpt.com", ("chatgpt.com", "")),
    ("https://example.com", ("example.com", "")),
    ("https://example.com/", ("example.com", "")),
])
def test_parse_valid_urls(url: str, expected: tuple[str, str]):
    assert _parse_meeting_url(url) == expected


@pytest.mark.parametrize("url", [
    "",  # empty
    "not a url",  # no scheme / no dots
    "https://",  # empty host
    "/just/a/path",  # no host
])
def test_parse_rejects_invalid(url: str):
    assert _parse_meeting_url(url) is None


# ---- _url_pattern ------------------------------------------------------


def test_url_pattern_non_strict_matches_any_room():
    rx = _url_pattern("meet.google.com", "/abc-defg-hij", strict=False)
    assert re.search(rx, "https://meet.google.com/xyz-pqrs-tuv")
    assert re.search(rx, "https://meet.google.com/a/b/c")
    # Does not match other hosts.
    assert not re.search(rx, "https://evil.com/meet.google.com/")
    # Anchored — a URL with meet.google.com as a substring mid-path doesn't match.
    assert not re.search(rx, "https://news.ycombinator.com/?url=meet.google.com/abc")


def test_url_pattern_strict_matches_only_that_room():
    rx = _url_pattern("meet.google.com", "/abc-defg-hij", strict=True)
    assert re.search(rx, "https://meet.google.com/abc-defg-hij")
    assert not re.search(rx, "https://meet.google.com/other-room")


def test_url_pattern_escapes_regex_metacharacters():
    rx = _url_pattern("a.b.c.example.com", "/room.1+2", strict=True)
    # Literal dot / plus — not regex-any.
    assert re.search(rx, "https://a.b.c.example.com/room.1+2")
    # Dots must be literal — "aXbXcXexampleXcom" shouldn't match.
    assert not re.search(rx, "https://aXbXcXexampleXcom/room.1+2")


# ---- _unique_app_key ---------------------------------------------------


@pytest.mark.parametrize("seed, taken, expected", [
    # Plain Windows executable.
    ("loom.exe", [], "loom"),
    ("RCMeetings.exe", [], "rcmeetings"),
    # macOS bundle id — strip common prefixes.
    ("com.hnc.Discord", [], "hnc-discord"),
    ("us.zoom.xos", [], "zoom-xos"),
    # Collisions → -2, -3, ...
    ("loom.exe", ["loom"], "loom-2"),
    ("loom.exe", ["loom", "loom-2"], "loom-3"),
    # Fallback when seed has no valid chars.
    ("?!?", [], "custom"),
])
def test_unique_app_key(seed: str, taken: list[str], expected: str):
    assert _unique_app_key(seed, taken) == expected


def test_unique_app_key_returns_ascii_slug():
    """Keys are used in JSON + env vars — must contain only lower-ascii +
    digits + hyphens."""
    key = _unique_app_key("Some Weird App™ 3.0", [])
    assert re.fullmatch(r"[a-z0-9-]+", key)


# ---- _friendly_url_pattern ---------------------------------------------


@pytest.mark.parametrize("pattern, expected", [
    # Google Meet default
    (r"^https://meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}",
     "meet.google.com/…"),
    # Zoom subdomain wildcard
    (r"^https://[^/]+\.zoom\.us/wc/join/", "*.zoom.us/wc/join"),
    # Whereby
    (r"^https://whereby\.com/[^/]+", "whereby.com/…"),
    # Strict path — no trailing ellipsis
    (r"^https://meet\.google\.com/abc-defg-hij",
     "meet.google.com/abc-defg-hij"),
    # Zoom j/<id>
    (r"^https://[^/]+\.zoom\.us/j/\d+", "*.zoom.us/j/…"),
])
def test_friendly_url_pattern(pattern: str, expected: str):
    assert _friendly_url_pattern(pattern) == expected


def test_friendly_url_pattern_unknown_falls_through():
    """Unrecognised pattern passes through unchanged rather than being
    mangled into garbage."""
    raw = "weird-garbage-not-a-url"
    assert _friendly_url_pattern(raw) == raw


# ---- _display_name_from_host -------------------------------------------


@pytest.mark.parametrize("host, expected", [
    ("meet.google.com", "Google Meet"),
    ("zoom.us", "Zoom"),
    ("whereby.com", "Whereby"),
    ("meet.jit.si", "Jitsi Meet"),
    # Unknown site — middle label, capitalized.
    ("loom.com", "Loom"),
    ("tryclassroom.app", "Tryclassroom"),
])
def test_display_name_from_host(host: str, expected: str):
    assert _display_name_from_host(host) == expected
