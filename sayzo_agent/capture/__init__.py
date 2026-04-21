"""Audio capture sources (microphone + system loopback)."""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Platform dispatch: export the right SystemCapture for the current OS.
# ---------------------------------------------------------------------------
import logging as _logging
import platform as _platform
import sys as _sys

_log = _logging.getLogger(__name__)


def _mac_version_tuple() -> tuple[int, ...]:
    raw = _platform.mac_ver()[0] or "0"
    try:
        return tuple(int(p) for p in raw.split("."))
    except ValueError:
        return (0,)


if _sys.platform == "darwin":
    # The macOS helper uses CoreAudio Process Taps (AudioHardwareCreateProcessTap),
    # introduced in macOS 14.4. Older versions can't run the helper at all.
    if _mac_version_tuple() < (14, 4):
        _log.error(
            "macOS %s is below the 14.4 minimum required for system audio "
            "capture; SystemCapture disabled",
            _platform.mac_ver()[0],
        )
        SystemCapture = None  # type: ignore[assignment]
    else:
        from .system_mac import SystemCapture as SystemCapture
elif _sys.platform == "win32":
    from .system_win import SystemCapture as SystemCapture
else:
    SystemCapture = None  # type: ignore[assignment]
