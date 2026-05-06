"""Per-process mic-attribution wrapper around the ``audio-detect`` Swift binary.

Why this module exists:

The current macOS code in :mod:`platform_mac` has historically claimed
"we can't attribute mic-in-use to a specific process cheaply." That was
true on macOS < 14.4. Since 14.4, CoreAudio exposes
``kAudioHardwarePropertyProcessObjectList`` + sibling per-process
properties (``IsRunningInput``, etc.) that give Windows-equivalent
attribution. The catch: on macOS 26 (Tahoe) those APIs return
``kAudioHardwareUnknownPropertyError`` ('who?') when called from an
unsigned Python ctypes binding, but work fine from a bare Swift binary
— almost certainly a Hardened Runtime / signing check on the caller.

So we ship a small Swift CLI (``arm/audio-detect/main.swift``, compiled
in CI), call it via subprocess, parse the JSON. Output is one row per
``AudioProcessObject`` with PID, responsible PID (Apple's privacy-
attribution source), bundle id, and the IsRunningInput / Output /
Running flags.

Cached for a short window so the watcher's 2 s poll doesn't double-cost
on the helper invocation.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# How long a snapshot stays cached. The arm subsystem polls at
# ArmConfig.poll_interval_secs (default 2 s); 1.8 s sits just under the
# poll interval so consecutive watcher polls reuse the same snapshot
# (one subprocess invocation per poll instead of two), with enough
# headroom that asyncio scheduling jitter doesn't cause a cache miss.
# Settings UI calls bypass with ``force_refresh=True``.
_CACHE_TTL_SECS = 1.8


@dataclass(frozen=True)
class AudioProcess:
    """One row from ``audio-detect --json``."""

    pid: int
    responsible_pid: int  # -1 when the SPI didn't resolve
    bundle_id: Optional[str]
    input: bool
    output: bool
    running: bool


@dataclass
class _Cache:
    snapshot: list[AudioProcess] = field(default_factory=list)
    expires_at: float = 0.0


_cache = _Cache()
_cache_lock = threading.Lock()


def _binary_path() -> Optional[Path]:
    """Locate the audio-detect helper.

    Lookup order, mirroring how :mod:`capture.system_mac` finds audio-tap:

    1. PyInstaller-frozen layout: ``sys._MEIPASS/sayzo_agent/arm/audio-detect/``.
    2. Dev layout (running from the repo): same path relative to this
       file's package directory.
    3. Anything on ``$PATH`` named ``audio-detect`` (escape hatch for
       hand-installed builds).

    Returns None if nothing executable was found — callers must handle
    this and degrade gracefully (e.g. return an empty holders list and
    log once).
    """
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "sayzo_agent/arm/audio-detect/audio-detect")

    candidates.append(Path(__file__).parent / "audio-detect" / "audio-detect")

    from shutil import which
    on_path = which("audio-detect")
    if on_path:
        candidates.append(Path(on_path))

    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return None


_binary_warn_fired = False
_binary_path_logged: Optional[Path] = None
_stderr_seen: set[str] = set()


def _log_binary_path_once(path: Path) -> None:
    global _binary_path_logged
    if _binary_path_logged == path:
        return
    _binary_path_logged = path
    log.info("[arm.audio_detect] using binary: %s", path)


def _log_stderr_once(msg: str) -> None:
    if msg in _stderr_seen:
        return
    _stderr_seen.add(msg)
    log.info("[arm.audio_detect] stderr: %s", msg)


def _warn_missing_binary_once() -> None:
    global _binary_warn_fired
    if _binary_warn_fired:
        return
    _binary_warn_fired = True
    log.warning(
        "[arm.audio_detect] audio-detect binary not found on disk. "
        "Build it with: cd sayzo_agent/arm/audio-detect && "
        "swiftc -O -target arm64-apple-macos14.4 -o audio-detect main.swift "
        "-framework CoreAudio -framework Foundation. macOS meeting detection "
        "will be silent until then."
    )


def _run_binary(binary: Path, timeout_secs: float = 1.5) -> list[AudioProcess]:
    """Invoke ``audio-detect --json`` and parse the output.

    Returns an empty list on any failure (timeout, non-zero exit,
    malformed JSON). All failure modes are logged at WARNING the first
    time and DEBUG thereafter — we never raise back to the watcher.
    """
    try:
        proc = subprocess.run(
            [str(binary), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired:
        log.warning("[arm.audio_detect] %s --json timed out after %.1fs",
                    binary.name, timeout_secs)
        return []
    except OSError as exc:
        log.warning("[arm.audio_detect] launch %s failed: %s", binary, exc)
        return []

    if proc.stderr.strip():
        # Swift binary writes diagnostic warnings (e.g. ProcessObjectList
        # failures, signing complaints) to stderr. Log once-per-distinct
        # message at INFO so production diagnostics surface without
        # needing DEBUG, but a long meeting doesn't spam.
        _log_stderr_once(proc.stderr.strip())

    if proc.returncode != 0:
        log.warning("[arm.audio_detect] %s exited %d (stderr: %r)",
                    binary.name, proc.returncode, proc.stderr.strip())
        return []

    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log.debug("[arm.audio_detect] non-JSON output: %s (first 200: %r)",
                  exc, proc.stdout[:200])
        return []

    out: list[AudioProcess] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            out.append(AudioProcess(
                pid=int(r.get("pid", -1)),
                responsible_pid=int(r.get("responsible_pid", -1)),
                bundle_id=r.get("bundle_id"),
                input=bool(r.get("input", 0)),
                output=bool(r.get("output", 0)),
                running=bool(r.get("running", 0)),
            ))
        except (TypeError, ValueError):
            log.debug("[arm.audio_detect] skip malformed row: %r", r)
            continue
    return out


def snapshot(*, force_refresh: bool = False) -> list[AudioProcess]:
    """Return the current audio-process list, possibly served from cache.

    ``force_refresh=True`` skips the cache (used by tests + the Settings
    UI's live mic-holder picker, which needs fresh data on user click).
    """
    if sys.platform != "darwin":
        return []

    now = time.monotonic()
    if not force_refresh:
        with _cache_lock:
            if now < _cache.expires_at:
                return list(_cache.snapshot)

    binary = _binary_path()
    if binary is None:
        _warn_missing_binary_once()
        with _cache_lock:
            _cache.snapshot = []
            _cache.expires_at = now + _CACHE_TTL_SECS
        return []

    _log_binary_path_once(binary)
    rows = _run_binary(binary)
    with _cache_lock:
        _cache.snapshot = rows
        _cache.expires_at = now + _CACHE_TTL_SECS
    return list(rows)


def reset_cache() -> None:
    """Clear the snapshot cache + missing-binary warn-once flag (test helper)."""
    global _binary_warn_fired
    with _cache_lock:
        _cache.snapshot = []
        _cache.expires_at = 0.0
    _binary_warn_fired = False
