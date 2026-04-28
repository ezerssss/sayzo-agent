"""Shared detector / URL helpers for the Meeting Apps surfaces.

The Settings Meeting Apps pane and the Add-app dialog both need the same
URL parsing, ``app_key`` slug generation, and friendly-display helpers.
Pure logic, no I/O, no GUI deps — directly importable from the bridge or
any future surface that needs to compose / validate detector specs.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse


_APP_KEY_STRIP = re.compile(r"[^a-z0-9]+")
_APP_KEY_TRIM_SUFFIXES = (".exe", ".app")
_APP_KEY_TRIM_PREFIXES = ("com.", "org.", "us.", "io.", "net.", "co.")


def unique_app_key(seed: str, taken: Iterable[str]) -> str:
    """Return a stable, sluggy ``app_key`` derived from ``seed``.

    App keys are used for cooldown bucketing and must be unique across
    the whitelist. Strips common executable / bundle-id prefixes +
    suffixes so ``loom.exe`` → ``loom`` and ``com.hnc.Discord`` →
    ``discord``. On collision, appends ``-2``, ``-3``, etc.
    """
    s = seed.lower().strip()
    for suffix in _APP_KEY_TRIM_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    for prefix in _APP_KEY_TRIM_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    base = _APP_KEY_STRIP.sub("-", s).strip("-") or "custom"
    taken_set = set(taken)
    if base not in taken_set:
        return base
    i = 2
    while f"{base}-{i}" in taken_set:
        i += 1
    return f"{base}-{i}"


def parse_meeting_url(url: str) -> Optional[tuple[str, str]]:
    """Extract ``(host, path)`` from a user-pasted meeting URL.

    Returns ``None`` only when the URL has no usable host (empty string,
    ``https://`` with nothing after it, a path-only input like
    ``/just/a/path``). Bare domains like ``chatgpt.com`` are accepted and
    returned as ``(host, "")`` — the caller decides how to treat an empty
    path (non-strict matches the whole host either way; strict needs a
    path and should reject an empty one at submit time).
    """
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return None
    path = parsed.path or ""
    if path == "/":
        path = ""
    if path.endswith("/"):
        path = path.rstrip("/")
    return host, path


def url_pattern(host: str, path: str, *, strict: bool) -> str:
    """Build a URL regex that matches ``host/path`` (or the whole host
    section when ``strict=False``).

    Strict matches the exact meeting room (single-room users). Non-strict
    matches any path under the host — e.g. Google Meet's
    ``meet.google.com/abc-defg-hij`` becomes
    ``^https://meet\\.google\\.com/`` so every room counts.
    """
    host_re = re.escape(host)
    if strict:
        path_re = re.escape(path)
        return rf"^https://{host_re}{path_re}"
    return rf"^https://{host_re}/"


_GENERIC_LABELS = frozenset({
    "www", "app", "m", "mobile", "web", "go",
})


def title_pattern_from_host(host: str) -> Optional[str]:
    """Build a window-title regex that matches the host's product name.

    Required for macOS Web specs: ``platform_mac.get_browser_window_urls``
    always returns ``[]`` (avoids the Automation TCC dialog), so a custom
    spec with only ``url_patterns`` can never match on Mac. Auto-deriving
    a title pattern from the host means user-added Web specs still work
    on macOS via ``get_browser_window_titles`` + the matcher's
    title-pattern fallback.

    Picks the most distinctive label of the host: ``chatgpt`` from
    ``chatgpt.com``, ``gemini`` from ``gemini.google.com``, ``notion``
    from ``app.notion.com``. Returns a case-insensitive word-bounded
    regex (e.g. ``r"(?i)\\bchatgpt\\b"``) — title bars typically contain
    the product name and the word boundary keeps the match precise
    without requiring the user to think about regex.

    Returns ``None`` when the host is too generic to safely auto-derive
    (e.g. ``google.com``); the caller should leave ``title_patterns``
    empty in that case.
    """
    parts = [p for p in host.lower().split(".") if p and p not in _GENERIC_LABELS]
    if len(parts) >= 2:
        parts = parts[:-1]  # drop TLD
    if not parts:
        return None
    label = parts[0]
    if not label or len(label) < 3:
        return None
    return rf"(?i)\b{re.escape(label)}\b"


def host_from_url_pattern(pattern: str) -> Optional[str]:
    """Reverse-parse the host out of a regex emitted by ``url_pattern``.

    ``url_pattern(host, path, strict=False)`` returns
    ``"^https://{re.escape(host)}/"``. This walks that string, treating
    backslash-escapes literally, and stops at the first unescaped slash.
    Used by ``add_detector`` to auto-fill ``title_patterns`` without
    needing the front-end to send a separate host alongside the regex.

    Returns ``None`` for non-conforming patterns (no ``^https://``
    prefix, or no host before a slash).
    """
    if not pattern.startswith("^https://"):
        return None
    rest = pattern[len("^https://"):]
    host_chars: list[str] = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "/":
            break
        if ch == "\\" and i + 1 < len(rest):
            host_chars.append(rest[i + 1])
            i += 2
            continue
        host_chars.append(ch)
        i += 1
    host = "".join(host_chars)
    if not host or "." not in host:
        return None
    return host


_KNOWN_HOST_NAMES: dict[str, str] = {
    "meet.google.com": "Google Meet",
    "teams.microsoft.com": "Microsoft Teams",
    "teams.live.com": "Microsoft Teams",
    "zoom.us": "Zoom",
    "whereby.com": "Whereby",
    "meet.jit.si": "Jitsi Meet",
    "8x8.vc": "8x8 Meet",
}


def display_name_from_host(host: str) -> str:
    """Guess a display name from a hostname — used to pre-fill the web
    tab's name field.

    ``meet.google.com`` → ``Google Meet``; ``zoom.us`` → ``Zoom``;
    ``whereby.com`` → ``Whereby``. Falls back to the middle hostname
    label for unknown sites.
    """
    h = host.lower()
    if h in _KNOWN_HOST_NAMES:
        return _KNOWN_HOST_NAMES[h]
    for k, v in _KNOWN_HOST_NAMES.items():
        if h.endswith("." + k) or h.endswith(k):
            return v
    parts = [p for p in h.split(".") if p not in ("www", "app", "meet")]
    if len(parts) >= 2:
        base = parts[-2]
    elif len(parts) == 1:
        base = parts[0]
    else:
        return h
    return base[:1].upper() + base[1:]


def friendly_url_pattern(pattern: str) -> str:
    """Strip regex glyphs from a URL pattern for display.

    The stored patterns look like ``^https://meet\\.google\\.com/``; users
    shouldn't have to read regex. Returns ``meet.google.com/…``.
    Best-effort — unrecognised patterns fall back to the raw string so
    we never render garbage.
    """
    out = pattern
    for prefix in ("^https://", "^http://", "https://", "http://", "^"):
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    out = re.sub(r"\[\^/\]\+(?=\\?\.)", "*", out)
    out = re.sub(r"\[[^\]]+\][+*]?(?:\{\d+,?\d*\})?", "…", out)
    out = re.sub(r"\{\d+,?\d*\}", "…", out)
    out = out.replace("\\.", ".").replace("\\-", "-").replace("\\/", "/")
    out = out.replace(".+", "…").replace(".*", "…")
    out = re.sub(r"\\[dws]\+", "…", out)
    out = out.rstrip("$").rstrip("/")
    out = re.sub(r"(?:…[-/]?){2,}", "…", out)
    if not out:
        return pattern
    return out
