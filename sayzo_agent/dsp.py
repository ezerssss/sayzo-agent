"""Post-capture audio DSP applied at session close, before Opus encoding.

Pipeline per channel:

    mic:    int16 -> float32 -> highpass(80Hz) -> denoise -> peak-norm -> int16
    system: int16 -> float32 -> highpass(40Hz) ->         -> peak-norm -> int16

This runs on the heavy-worker ``ThreadPoolExecutor`` so it never blocks the
asyncio loop. Transcription and speaker embedding read the *raw*
``buffers.mic_pcm`` upstream, so DSP here has zero impact on STT quality — it
only changes what ends up on disk + uploaded.

All stages are controlled by ``CaptureConfig`` flags; ``dsp_enabled=False``
restores the raw-PCM path exactly.
"""
from __future__ import annotations

import logging

import numpy as np
from scipy.signal import butter, sosfilt

from .config import CaptureConfig

log = logging.getLogger(__name__)


try:
    import noisereduce as _nr  # type: ignore
except Exception as e:  # pragma: no cover - import-time fallback
    _nr = None
    log.warning("[dsp] noisereduce unavailable (%s); denoise will be skipped", e)


# Cache SOS coefficients keyed by (cutoff_hz, sr, order). Sample rate +
# cutoffs are constant across sessions, so this is a 2-entry dict in practice.
_BUTTER_CACHE: dict[tuple[float, int, int], np.ndarray] = {}


def _butter_highpass(cutoff_hz: float, sr: int, order: int = 4) -> np.ndarray:
    key = (cutoff_hz, sr, order)
    sos = _BUTTER_CACHE.get(key)
    if sos is None:
        sos = butter(order, cutoff_hz, btype="highpass", fs=sr, output="sos")
        _BUTTER_CACHE[key] = sos
    return sos


def _apply_highpass(x: np.ndarray, cutoff_hz: float, sr: int) -> np.ndarray:
    if cutoff_hz <= 0 or x.size == 0:
        return x
    sos = _butter_highpass(cutoff_hz, sr)
    return sosfilt(sos, x).astype(np.float32, copy=False)


def _peak_normalize(x: np.ndarray, target_dbfs: float) -> np.ndarray:
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x  # silent — don't amplify noise
    target = 10.0 ** (target_dbfs / 20.0)
    gain = target / peak
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
    if _nr is None or x.size == 0:
        return x
    try:
        out = _nr.reduce_noise(
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
    """
    if not cfg.dsp_enabled or not pcm16:
        return pcm16
    x = _i16_to_f32(pcm16)
    x = _apply_highpass(x, cfg.highpass_mic_hz, sr)
    if cfg.denoise_enabled:
        x = _denoise(x, sr, cfg.denoise_strength)
    x = _peak_normalize(x, cfg.peak_normalize_dbfs)
    return _f32_to_i16(x)


def apply_sys_dsp(pcm16: bytes, sr: int, cfg: CaptureConfig) -> bytes:
    """Light DSP for the system-audio channel.

    System audio is typically already a clean digital stream (Zoom, Discord,
    a YouTube video). Aggressive denoising would damage music or low-volume
    speech from the far side, so this chain is intentionally light: just a
    low-cutoff highpass to kill DC/rumble and a peak normalize for
    consistent loudness.
    """
    if not cfg.dsp_enabled or not pcm16:
        return pcm16
    x = _i16_to_f32(pcm16)
    x = _apply_highpass(x, cfg.highpass_sys_hz, sr)
    x = _peak_normalize(x, cfg.peak_normalize_dbfs)
    return _f32_to_i16(x)
