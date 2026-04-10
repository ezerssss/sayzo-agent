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
