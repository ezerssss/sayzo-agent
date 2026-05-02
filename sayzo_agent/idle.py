"""User-input idle-time query.

Sync, cheap, side-effect-free. The daily-drill scheduler calls this on each
60s tick to decide whether the user is currently active (don't fire) or has
been away from the keyboard/mouse long enough that an OS notification will
be received calmly rather than as a mid-task interruption.

Per-platform implementation:

* **Windows** — ``win32api.GetLastInputInfo()`` returns the system tick
  at which the last keyboard / mouse / pen / touch event was processed by
  the input subsystem. Subtracting from ``GetTickCount()`` gives the idle
  duration in milliseconds. ~1 microsecond call cost. pywin32 already a dep.
* **macOS** — Quartz's ``CGEventSourceSecondsSinceLastEventType`` with
  ``kCGAnyInputEventType`` returns idle seconds directly. Honors HID
  events from any source. ~1 microsecond call cost. Requires
  ``pyobjc-framework-Quartz`` (added in pyproject.toml).
* **Other platforms / import failure** — returns ``0.0`` so the caller's
  "user is idle?" check fails closed. Better to skip a notification fire
  than to interrupt an active user because we couldn't query.

Anything that raises here returns ``0.0``; the failure is logged at debug
level so log spam stays bounded across the per-tick polling loop.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


# Cache the platform-specific implementation choice on first call so we
# don't repeat the import probe every tick.
_impl: "_IdleImpl | None" = None


class _IdleImpl:
    """Stateless probe — instances exist only so we can swap them in tests."""

    def get(self) -> float:
        return 0.0


class _WindowsIdle(_IdleImpl):
    def __init__(self) -> None:
        # Import once at construction; cache the bound functions so each
        # call avoids module-attribute lookup overhead.
        import win32api  # type: ignore[import-not-found]

        self._get_tick = win32api.GetTickCount
        self._get_last = win32api.GetLastInputInfo

    def get(self) -> float:
        try:
            tick = self._get_tick()
            last = self._get_last()
            # GetTickCount wraps every ~49.7 days. Same with GetLastInputInfo
            # (both DWORD). Modular subtraction handles wrap correctly.
            delta_ms = (tick - last) & 0xFFFFFFFF
            return delta_ms / 1000.0
        except Exception:
            log.debug("[idle] Windows query failed", exc_info=True)
            return 0.0


class _MacIdle(_IdleImpl):
    def __init__(self) -> None:
        from Quartz import (  # type: ignore[import-not-found]
            CGEventSourceSecondsSinceLastEventType,
            kCGAnyInputEventType,
            kCGEventSourceStateCombinedSessionState,
        )

        self._fn = CGEventSourceSecondsSinceLastEventType
        self._state = kCGEventSourceStateCombinedSessionState
        self._evt_any = kCGAnyInputEventType

    def get(self) -> float:
        try:
            return float(self._fn(self._state, self._evt_any))
        except Exception:
            log.debug("[idle] macOS query failed", exc_info=True)
            return 0.0


def _make_impl() -> _IdleImpl:
    if sys.platform == "win32":
        try:
            return _WindowsIdle()
        except Exception:
            log.warning(
                "[idle] win32api unavailable; treating user as always-active",
                exc_info=True,
            )
            return _IdleImpl()
    if sys.platform == "darwin":
        try:
            return _MacIdle()
        except Exception:
            log.warning(
                "[idle] Quartz unavailable; treating user as always-active",
                exc_info=True,
            )
            return _IdleImpl()
    log.info(
        "[idle] no idle-query backend for platform=%s; treating user as always-active",
        sys.platform,
    )
    return _IdleImpl()


def get_idle_seconds() -> float:
    """Return seconds since last keyboard / mouse input.

    ``0.0`` when the platform is unsupported or the OS query failed — the
    caller should treat that as "user is active, do not fire."
    """
    global _impl
    if _impl is None:
        _impl = _make_impl()
    return _impl.get()


def reset_for_tests() -> None:
    """Drop the cached impl so tests can install a fake under sys.modules."""
    global _impl
    _impl = None
