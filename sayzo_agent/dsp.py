"""Post-capture audio DSP applied at session close, before Opus encoding.

Pipeline per channel:

    mic:    int16 -> float32 -> highpass(80Hz) -> denoise -> [peak-norm] -> int16
    system: int16 -> float32 -> highpass(40Hz) ->         -> [peak-norm] -> int16

The bracketed per-channel peak-normalize runs only when
``loudness_match_enabled=False``. By default (v3.22+) loudness is handled jointly
across both channels by ``loudness.match_loudness`` after session trim, so the
mic and system channels end at the same PERCEIVED loudness (peak-normalize only
matches peaks, not loudness).

This runs on the heavy-worker ``ThreadPoolExecutor`` so it never blocks the
asyncio loop. Output is what ends up on disk + uploaded to the server for
transcription.

All stages are controlled by ``CaptureConfig`` flags; ``dsp_enabled=False``
restores the raw-PCM path exactly.
"""
from __future__ import annotations

import logging
import typing

import numpy as np

from .config import CaptureConfig

log = logging.getLogger(__name__)


# noisereduce tries to import torch at module level for its torchgate path
# (guarded try/except — a no-op since v3.17 removed torch from the bundle,
# but in a dev venv that still has torch installed it costs ~4 s + ~150 MB
# RSS). Defer the load until the first denoise call so the agent's
# "Starting…" phase doesn't pay it. ``_NR_SENTINEL`` distinguishes "not yet
# tried" from "tried, failed".
_NR_SENTINEL = object()
_nr: typing.Any = _NR_SENTINEL


def _get_noisereduce():
    global _nr
    if _nr is not _NR_SENTINEL:
        return _nr
    try:
        import noisereduce as nr  # type: ignore
        _nr = nr
    except Exception as e:
        log.warning("[dsp] noisereduce unavailable (%s); denoise will be skipped", e)
        _nr = None
    return _nr


# Cache SOS coefficients keyed by (cutoff_hz, sr, order). Sample rate +
# cutoffs are constant across sessions, so this is a 2-entry dict in practice.
_BUTTER_CACHE: dict[tuple[float, int, int], np.ndarray] = {}


def _butter_highpass(cutoff_hz: float, sr: int, order: int = 4) -> np.ndarray:
    key = (cutoff_hz, sr, order)
    sos = _BUTTER_CACHE.get(key)
    if sos is None:
        # scipy.signal pulls ~60 MB of stats / special / signal at import; defer
        # to first session-close so it doesn't sit in the boot path.
        from scipy.signal import butter
        sos = butter(order, cutoff_hz, btype="highpass", fs=sr, output="sos")
        _BUTTER_CACHE[key] = sos
    return sos


def _apply_highpass(x: np.ndarray, cutoff_hz: float, sr: int) -> np.ndarray:
    if cutoff_hz <= 0 or x.size == 0:
        return x
    from scipy.signal import sosfilt
    sos = _butter_highpass(cutoff_hz, sr)
    return sosfilt(sos, x).astype(np.float32, copy=False)


def _peak_normalize(
    x: np.ndarray, target_dbfs: float, max_gain_db: float | None = None,
) -> np.ndarray:
    """Scale ``x`` so its peak hits ``target_dbfs``, with optional gain cap.

    ``max_gain_db`` (v3.6.4+) caps the amount of amplification applied. When
    AEC has reduced the dominant signal RMS, an uncapped peak-normalize would
    apply large gain to reach the target — amplifying constant background
    (fan hum, room tone) into audibility along with the legitimate content.
    With the cap, quiet captures emit below the target rather than getting
    pathologically lifted. ``None`` (test default) restores the pre-v3.6.4
    uncapped behavior.
    """
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x  # silent — don't amplify noise
    target = 10.0 ** (target_dbfs / 20.0)
    gain = target / peak
    if max_gain_db is not None:
        max_gain = 10.0 ** (max_gain_db / 20.0)
        gain = min(gain, max_gain)
    # Don't pointlessly multiply if we're already there.
    if 0.99 <= gain <= 1.01:
        return x
    return np.clip(x * gain, -1.0, 1.0).astype(np.float32, copy=False)


def _i16_to_f32(pcm16: bytes) -> np.ndarray:
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


def _f32_to_i16(x: np.ndarray) -> bytes:
    clipped = np.clip(x, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def _denoise(x: np.ndarray, sr: int, strength: float) -> np.ndarray:
    if x.size == 0:
        return x
    nr = _get_noisereduce()
    if nr is None:
        return x
    try:
        out = nr.reduce_noise(
            y=x,
            sr=sr,
            stationary=True,
            prop_decrease=float(np.clip(strength, 0.0, 1.0)),
        )
        return out.astype(np.float32, copy=False)
    except Exception as e:
        log.warning("[dsp] noisereduce failed (%s); returning pre-denoise signal", e)
        return x


def apply_mic_dsp(pcm16: bytes, sr: int, cfg: CaptureConfig) -> bytes:
    """Full DSP chain for the mic channel.

    With ``cfg.dsp_enabled=False`` the input bytes are returned unchanged.
    Everything else is controlled by individual flags / cutoffs so the chain
    can be partially disabled (e.g. ``highpass_mic_hz=0`` + ``denoise_enabled=False``
    + ``peak_normalize_dbfs=0`` would be identity modulo quantization round-trip).

    Loudness: when ``loudness_match_enabled`` (v3.22+ default), the per-channel
    peak-normalize here is SKIPPED — the joint ``loudness.match_loudness`` stage
    (run after trim, on both channels together) owns final loudness so mic and
    system end at the same perceived level. When it's off, the per-channel
    peak-normalize runs as before (exact pre-v3.22 behavior).
    """
    if not cfg.dsp_enabled or not pcm16:
        return pcm16
    x = _i16_to_f32(pcm16)
    x = _apply_highpass(x, cfg.highpass_mic_hz, sr)
    if cfg.denoise_enabled:
        x = _denoise(x, sr, cfg.denoise_strength)
    if not cfg.loudness_match_enabled:
        x = _peak_normalize(x, cfg.peak_normalize_dbfs, cfg.peak_normalize_max_gain_db)
    return _f32_to_i16(x)


def apply_sys_dsp(pcm16: bytes, sr: int, cfg: CaptureConfig) -> bytes:
    """Light DSP for the system-audio channel.

    System audio is typically already a clean digital stream (Zoom, Discord,
    a YouTube video). Aggressive denoising would damage music or low-volume
    speech from the far side, so this chain is intentionally light: just a
    low-cutoff highpass to kill DC/rumble (loudness is handled jointly — see
    ``apply_mic_dsp`` and ``loudness.match_loudness``).

    Loudness: when ``loudness_match_enabled`` (v3.22+ default), the per-channel
    peak-normalize here is SKIPPED in favor of the joint loudness-match stage;
    when it's off, the per-channel peak-normalize runs as before.
    """
    if not cfg.dsp_enabled or not pcm16:
        return pcm16
    x = _i16_to_f32(pcm16)
    x = _apply_highpass(x, cfg.highpass_sys_hz, sr)
    if not cfg.loudness_match_enabled:
        x = _peak_normalize(x, cfg.peak_normalize_dbfs, cfg.peak_normalize_max_gain_db)
    return _f32_to_i16(x)
