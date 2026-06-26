"""Joint inter-channel loudness matching, applied at session close AFTER
session trim and BEFORE Opus encode.

The per-channel ``dsp._peak_normalize`` matches PEAKS, not perceived loudness:
the mic (L) and system (R) channels can both hit the peak target yet sound at
very different volumes on replay (one side of the conversation drowning the
other). This module replaces that with a single stage that sees BOTH channels
and equalizes their PERCEIVED loudness.

Why LUFS (ITU-R BS.1770, via ``pyloudnorm``): it is K-weighted (models human
hearing) and *gated* (ignores silence). That gating is exactly right here —
mic and system are anti-correlated (one party talks while the other is silent),
so the measurement reflects each side's actual speech rather than being diluted
by how long the other side talked. Measuring AFTER trim means pyloudnorm only
sees the kept speech (pre/post silence + echo-zeroed mic spans are already gone).

Strategy (config-driven, meet-in-the-middle with a floor):
  - Both channels measurable -> target = clamp(midpoint, floor=
    ``loudness_target_lufs``, max=quieter+``loudness_max_boost_db``). The
    midpoint is lifted UP to the floor when it falls below it (so quiet captures
    don't get quieter and the louder far side isn't needlessly cut down),
    bounded by the boost cap so a near-silent post-AEC channel's hum/room-tone
    is never lifted past it (same rationale as dsp.py's peak-norm cap). When the
    gap exceeds 2*max_boost the target slides toward the quieter channel — so
    the two channels always end matched, never silently mismatched.
  - One channel silent (solo / mic-only sessions, kept since v3.21.0) -> there
    is no counterpart to meet; normalize the lone channel toward the SAME target
    (``loudness_target_lufs``, boost bounded, cut allowed) so mic-only and
    two-sided captures end at a consistent loudness, and leave the silent
    channel untouched.
  - A joint sample-peak ceiling then scales BOTH channels by the same factor if
    the louder one would clip, so nothing clips and the match is preserved.

Applies per-channel SCALAR gain only — no resampling/shifting — so mic<->sys
sample alignment (load-bearing for AEC, CLAUDE.md design rule 6) is untouched.

Runs on the heavy-worker ``ThreadPoolExecutor``.
``cfg.loudness_match_enabled=False`` makes this an identity pass (and dsp.py
falls back to its per-channel peak-normalize, restoring pre-v3.22 behavior).
Replay-UX only: server-side transcription does its own gain control.
"""
from __future__ import annotations

import logging
import typing
from dataclasses import dataclass

import numpy as np

from .config import CaptureConfig
from .dsp import _f32_to_i16, _i16_to_f32

log = logging.getLogger(__name__)


# Lazy load of pyloudnorm — same pattern as ``dsp._get_noisereduce`` /
# ``aec._get_apm``. pyloudnorm pulls scipy.signal (~60 MB at import); keep it
# out of the boot path. _SENTINEL distinguishes "not yet tried" from "tried,
# failed" so the import warning fires exactly once.
_PYLN_SENTINEL = object()
_pyln: typing.Any = _PYLN_SENTINEL

# Cache one BS.1770 meter per sample rate. ``integrated_loudness`` is a pure
# function of its input (no accumulated state across calls), so reuse is safe.
_METER_CACHE: dict[int, typing.Any] = {}

# Sentinel for "channel loudness unmeasurable" — silent / below the BS.1770
# absolute gate, or shorter than the 400 ms gating block. Kept distinct from a
# real, very-low loudness value so the gain math never multiplies by it.
_UNMEASURABLE = float("-inf")

_BLOCK_SECS = 0.4  # BS.1770 gating block; also the RMS-fallback window.


def _get_pyln():
    global _pyln
    if _pyln is not _PYLN_SENTINEL:
        return _pyln
    try:
        import pyloudnorm as pyln  # type: ignore
        _pyln = pyln
    except Exception as e:  # noqa: BLE001
        log.warning(
            "[loudness] pyloudnorm unavailable (%s); falling back to gated-RMS", e
        )
        _pyln = None
    return _pyln


@dataclass
class LoudnessReport:
    """Per-session summary for logs + record.json metadata (mirrors AecReport /
    TrimReport)."""

    enabled: bool
    ran: bool = False
    skip_reason: str = ""
    method: str = "lufs"            # effective method: "lufs" | "rms"
    fallback_used: bool = False     # asked for LUFS, got RMS (pyloudnorm missing)
    mic_lufs: float = _UNMEASURABLE
    sys_lufs: float = _UNMEASURABLE
    common_target_lufs: float = _UNMEASURABLE
    mic_gain_db: float = 0.0        # total applied gain (incl. joint ceiling)
    sys_gain_db: float = 0.0
    joint_attenuation_db: float = 0.0   # extra equal cut from the peak ceiling (<=0)
    ceiling_dbfs: float = 0.0
    mic_only: bool = False          # only one channel was measurable

    def as_metadata(self) -> dict:
        def _r(v: float):  # -inf is not valid JSON -> null
            return None if v == _UNMEASURABLE else round(v, 2)

        return {
            "enabled": self.enabled,
            "ran": self.ran,
            "skip_reason": self.skip_reason,
            "method": self.method,
            "fallback_used": self.fallback_used,
            "mic_lufs": _r(self.mic_lufs),
            "sys_lufs": _r(self.sys_lufs),
            "common_target_lufs": _r(self.common_target_lufs),
            "mic_gain_db": round(self.mic_gain_db, 2),
            "sys_gain_db": round(self.sys_gain_db, 2),
            "joint_attenuation_db": round(self.joint_attenuation_db, 2),
            "ceiling_dbfs": round(self.ceiling_dbfs, 2),
            "mic_only": self.mic_only,
        }


def _get_meter(sr: int, pyln):
    meter = _METER_CACHE.get(sr)
    if meter is None:
        meter = pyln.Meter(sr)  # builds the BS.1770 K-weighting for this rate
        _METER_CACHE[sr] = meter
    return meter


def _measure_lufs(x: np.ndarray, sr: int, pyln) -> float:
    """Mono integrated loudness, or ``_UNMEASURABLE`` for silent / <400 ms input.

    ``integrated_loudness`` returns ``-inf`` for silence/too-quiet (the -70 LUFS
    absolute gate) and raises ``ValueError`` for inputs shorter than the gating
    block. Both collapse to ``_UNMEASURABLE`` so the caller's gain math never
    sees NaN/inf. We deliberately do NOT call ``pyln.normalize.loudness`` (it
    would produce NaN/inf for these inputs) — gains are computed by the caller.
    """
    if x.size < int(_BLOCK_SECS * sr) + 1:
        return _UNMEASURABLE
    try:
        lufs = float(_get_meter(sr, pyln).integrated_loudness(x))
    except Exception as e:  # noqa: BLE001
        log.debug("[loudness] LUFS measure failed (%s)", e)
        return _UNMEASURABLE
    return lufs if np.isfinite(lufs) else _UNMEASURABLE


def _measure_rms_pseudo_lufs(x: np.ndarray, sr: int, abs_gate_db: float = -60.0) -> float:
    """numpy-only fallback used when pyloudnorm is unavailable.

    Gated RMS over 400 ms blocks (blocks below ``abs_gate_db`` dropped — the
    same "ignore silence" idea as BS.1770 gating, since the far side's talk time
    appears as silence on this channel even after trim), expressed in dBFS as a
    pseudo-LUFS proxy. The gain math only needs a measure that is (a) consistent
    across the two channels and (b) monotone with loudness, so any such proxy
    applied identically to both works for the relative match. (The solo target
    is then interpreted on this dBFS scale rather than true LUFS; acceptable for
    the fallback, which the healthcheck keeps out of shipped builds.)
    """
    if x.size == 0:
        return _UNMEASURABLE
    block = max(1, int(_BLOCK_SECS * sr))
    n_blocks = x.size // block
    if n_blocks == 0:
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        return 20.0 * np.log10(rms) if rms > 1e-9 else _UNMEASURABLE
    blocks = x[: n_blocks * block].astype(np.float64).reshape(n_blocks, block)
    block_rms = np.sqrt(np.mean(blocks**2, axis=1))
    block_db = 20.0 * np.log10(np.maximum(block_rms, 1e-12))
    keep = block_db > abs_gate_db
    if not np.any(keep):
        return _UNMEASURABLE
    gated_rms = float(np.sqrt(np.mean(block_rms[keep] ** 2)))
    return 20.0 * np.log10(gated_rms) if gated_rms > 1e-9 else _UNMEASURABLE


def match_loudness(
    mic_pcm16: bytes,
    sys_pcm16: bytes,
    sr: int,
    cfg: CaptureConfig,
) -> tuple[bytes, bytes, LoudnessReport]:
    """Equalize the perceived loudness of the mic + system channels.

    Returns ``(mic_out, sys_out, report)``. Output buffers are the same length
    as the inputs (scalar gain only); when disabled / unmeasurable the inputs
    are returned unchanged.
    """
    report = LoudnessReport(
        enabled=cfg.loudness_match_enabled,
        method=cfg.loudness_method,
        ceiling_dbfs=cfg.loudness_peak_ceiling_dbfs,
    )

    if not cfg.dsp_enabled:
        # DSP_ENABLED=0 is the documented "raw-PCM byte-for-byte" escape hatch
        # (config.py / CLAUDE.md). Loudness matching is part of session-close
        # processing, so it must honor it too.
        report.skip_reason = "dsp_disabled"
        return mic_pcm16, sys_pcm16, report
    if not cfg.loudness_match_enabled:
        report.skip_reason = "disabled"
        return mic_pcm16, sys_pcm16, report
    if not mic_pcm16 and not sys_pcm16:
        report.skip_reason = "empty_buffers"
        return mic_pcm16, sys_pcm16, report

    mic = _i16_to_f32(mic_pcm16)
    sys = _i16_to_f32(sys_pcm16)

    # ---- measure ----
    pyln = _get_pyln() if cfg.loudness_method == "lufs" else None
    if cfg.loudness_method == "lufs" and pyln is not None:
        mic_l = _measure_lufs(mic, sr, pyln)
        sys_l = _measure_lufs(sys, sr, pyln)
    else:
        report.method = "rms"
        report.fallback_used = cfg.loudness_method == "lufs"  # wanted LUFS, no lib
        mic_l = _measure_rms_pseudo_lufs(mic, sr)
        sys_l = _measure_rms_pseudo_lufs(sys, sr)
    report.mic_lufs, report.sys_lufs = mic_l, sys_l

    max_boost = cfg.loudness_max_boost_db
    mic_present = mic_l != _UNMEASURABLE
    sys_present = sys_l != _UNMEASURABLE

    if not mic_present and not sys_present:
        report.skip_reason = "both_silent"
        return mic_pcm16, sys_pcm16, report

    # ---- choose common target + per-channel gains ----
    mic_gain_db = 0.0
    sys_gain_db = 0.0
    floor = cfg.loudness_target_lufs
    if mic_present and sys_present:
        # Meet in the middle, but (a) lift the target UP to the floor when the
        # midpoint falls below it (so quiet captures don't get quieter and the
        # louder far side isn't needlessly cut), and (b) never boost the quieter
        # channel past max_boost. min(...) caps the boost; when the gap exceeds
        # 2*max_boost the target naturally slides toward the quieter side. Both
        # channels land on `target`, so they stay matched.
        t_mid = (mic_l + sys_l) / 2.0
        quieter = min(mic_l, sys_l)
        target = min(max(t_mid, floor), quieter + max_boost)
        mic_gain_db = target - mic_l   # one side cut, one side boosted -> matched
        sys_gain_db = target - sys_l
        report.common_target_lufs = target
    else:
        # Solo session: no counterpart to match. Normalize the lone channel
        # toward the target (boost bounded, cut allowed); leave the silent
        # channel byte-for-byte untouched. Same target as the two-channel floor
        # so mic-only and two-sided captures end at a consistent loudness.
        report.mic_only = True
        if mic_present:
            mic_gain_db = min(floor - mic_l, max_boost)
            report.common_target_lufs = mic_l + mic_gain_db
        else:
            sys_gain_db = min(floor - sys_l, max_boost)
            report.common_target_lufs = sys_l + sys_gain_db

    mic_lin = 10.0 ** (mic_gain_db / 20.0)
    sys_lin = 10.0 ** (sys_gain_db / 20.0)
    mic_out = mic * mic_lin
    sys_out = sys * sys_lin

    # ---- joint sample-peak ceiling (preserves the match: both scaled equally)
    ceiling_lin = 10.0 ** (cfg.loudness_peak_ceiling_dbfs / 20.0)
    peak = max(
        float(np.max(np.abs(mic_out))) if mic_out.size else 0.0,
        float(np.max(np.abs(sys_out))) if sys_out.size else 0.0,
    )
    joint_scale = 1.0
    if peak > ceiling_lin:
        joint_scale = ceiling_lin / peak
        mic_out = mic_out * joint_scale
        sys_out = sys_out * joint_scale
        report.joint_attenuation_db = 20.0 * float(np.log10(joint_scale))  # negative

    report.ran = True
    report.mic_gain_db = mic_gain_db + report.joint_attenuation_db
    report.sys_gain_db = sys_gain_db + report.joint_attenuation_db

    # A channel whose TOTAL gain is exactly unity is returned byte-for-byte
    # (int16->float32->int16 isn't identity — the 32768/32767 asymmetry can
    # shift samples by 1 LSB). This guarantees the silent channel of a solo
    # session, and any equal-loudness pair, comes out untouched.
    mic_bytes = mic_pcm16 if mic_lin * joint_scale == 1.0 else _f32_to_i16(mic_out)
    sys_bytes = sys_pcm16 if sys_lin * joint_scale == 1.0 else _f32_to_i16(sys_out)
    return mic_bytes, sys_bytes, report
