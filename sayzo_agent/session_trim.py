"""Final-audio slicing applied at session close, after DSP, before sink.

Replaces the earlier "zero-fill outside VAD windows + trailing-silence trim"
model. Slices both channels at ``[first_speech - pad, last_speech + pad]`` with
identical sample indices so mic↔sys alignment is preserved (load-bearing for
AEC and server-side multichannel diarization — see memory
``project_aec_misalignment_v3_6_0``). On the mic channel only, ``mic_echo_segments``
spans within the kept range are zero-filled, preserving CLAUDE.md design rule 5's
layered echo defense (AEC linear → echo_guard residual classifier → direct mic
zeroing → server-side ``isEchoLeakUtterance``).

Mid-conversation silences (thinking pauses, response latency, intra-turn
hesitation) are now kept as recorded audio. They're coachable signal, not dead
air.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import SpeechSegment


@dataclass
class TrimReport:
    original_secs: float
    kept_secs: float
    start_offset_secs: float
    end_offset_secs: float
    echo_zeroed_secs: float

    def as_metadata(self) -> dict:
        return {
            "original_secs": round(self.original_secs, 3),
            "kept_secs": round(self.kept_secs, 3),
            "start_offset_secs": round(self.start_offset_secs, 3),
            "end_offset_secs": round(self.end_offset_secs, 3),
            "echo_zeroed_secs": round(self.echo_zeroed_secs, 3),
        }


_EMPTY_REPORT = TrimReport(
    original_secs=0.0,
    kept_secs=0.0,
    start_offset_secs=0.0,
    end_offset_secs=0.0,
    echo_zeroed_secs=0.0,
)


def apply_session_trim(
    mic_pcm: bytes,
    sys_pcm: bytes,
    mic_segments: list[SpeechSegment],
    sys_segments: list[SpeechSegment],
    mic_echo_segments: list[SpeechSegment],
    pad_secs: float,
    sample_rate: int,
) -> tuple[bytes, bytes, TrimReport]:
    bytes_per_sample = 2
    sr = sample_rate
    original_secs = max(len(mic_pcm), len(sys_pcm)) / bytes_per_sample / sr

    all_segs = list(mic_segments) + list(sys_segments)
    if not all_segs:
        return b"", b"", TrimReport(
            original_secs=original_secs,
            kept_secs=0.0,
            start_offset_secs=0.0,
            end_offset_secs=original_secs,
            echo_zeroed_secs=0.0,
        )

    total_samples = min(len(mic_pcm), len(sys_pcm)) // bytes_per_sample
    if total_samples <= 0:
        return b"", b"", TrimReport(
            original_secs=original_secs,
            kept_secs=0.0,
            start_offset_secs=0.0,
            end_offset_secs=original_secs,
            echo_zeroed_secs=0.0,
        )

    first = min(s.start_ts for s in all_segs)
    last = max(s.end_ts for s in all_segs)

    start_sample = max(0, int((first - pad_secs) * sr))
    end_sample = min(total_samples, int((last + pad_secs) * sr))
    if end_sample <= start_sample:
        return b"", b"", TrimReport(
            original_secs=original_secs,
            kept_secs=0.0,
            start_offset_secs=start_sample / sr,
            end_offset_secs=max(0.0, original_secs - start_sample / sr),
            echo_zeroed_secs=0.0,
        )

    start_offset_secs = start_sample / sr
    byte_a = start_sample * bytes_per_sample
    byte_b = end_sample * bytes_per_sample
    # memoryview slice avoids the intermediate bytes copy that
    # `bytearray(mic_pcm[a:b])` / `bytes(sys_pcm[a:b])` would allocate —
    # session_close PCM can be 100+ MB per channel for multi-hour captures.
    mic_final = bytearray(memoryview(mic_pcm)[byte_a:byte_b])
    sys_final = bytes(memoryview(sys_pcm)[byte_a:byte_b])

    kept_samples = end_sample - start_sample
    kept_secs = kept_samples / sr
    end_offset_secs = max(0.0, original_secs - start_offset_secs - kept_secs)

    echo_zeroed_samples = 0
    silence = bytes(2)
    for seg in mic_echo_segments:
        a = max(0, int((seg.start_ts - start_offset_secs) * sr))
        b = min(kept_samples, int((seg.end_ts - start_offset_secs) * sr))
        if b <= a:
            continue
        mic_final[a * bytes_per_sample : b * bytes_per_sample] = silence * (b - a)
        echo_zeroed_samples += b - a

    return (
        bytes(mic_final),
        sys_final,
        TrimReport(
            original_secs=original_secs,
            kept_secs=kept_secs,
            start_offset_secs=start_offset_secs,
            end_offset_secs=end_offset_secs,
            echo_zeroed_secs=echo_zeroed_samples / sr,
        ),
    )
