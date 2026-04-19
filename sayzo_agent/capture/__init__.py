"""Audio capture sources (microphone + system loopback)."""
from __future__ import annotations

import numpy as np

# Target RMS for normalization (~-20 dBFS). This is loud enough for Whisper
# to transcribe accurately but not so loud that peaks clip.
_TARGET_RMS = 0.02
# Don't amplify frames quieter than this — they're silence/noise.
_NOISE_FLOOR_RMS = 1e-4
# Cap the gain to avoid blowing up a single quiet-but-not-silent frame.
_MAX_GAIN = 5.0


def normalize_rms(audio: np.ndarray) -> np.ndarray:
    """Scale audio so its RMS matches _TARGET_RMS.

    Skips near-silent audio (below noise floor) to avoid amplifying noise.
    Clamps gain to _MAX_GAIN and clips output to [-1, 1].
    """
    rms = float(np.sqrt(np.mean(audio * audio)))
    if rms < _NOISE_FLOOR_RMS:
        return audio
    gain = min(_TARGET_RMS / rms, _MAX_GAIN)
    if 0.95 <= gain <= 1.05:
        return audio  # already at target, skip the multiply
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


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
