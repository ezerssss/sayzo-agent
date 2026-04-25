"""Shared detector / URL helpers for the Meeting Apps surfaces.

The Settings Meeting Apps pane and the Add-app dialog both need the same
URL parsing, ``app_key`` slug generation, and friendly-display helpers that
the legacy tkinter ``settings_window.py`` carried inline. Lifted here so
the pywebview Settings bridge can use them without dragging tkinter
imports — and so the setup wizard (or any future surface) can reuse them
as the migration progresses.

Pure logic, no I/O, no GUI deps. Tested transitively via the Settings
bridge tests; the original behaviour is preserved verbatim.
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
