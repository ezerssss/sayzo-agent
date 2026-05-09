"""Live probe for the macOS audio-tap "captured audio is slowed down" bug.

Spawns the existing ``audio-tap`` Swift helper (no recompile required —
uses whatever's installed at /Applications/Sayzo.app/...), captures its
stdout for N seconds, parses the SAYZ-framed PCM, and writes WAV files
at multiple candidate sample rates. You play each WAV; the one that
sounds correct in real-time tells us the actual capture rate, which is
the one we should be writing.

Why
---
On macOS, the tap inherits the speaker output rate. When the user is on
a Bluetooth headset in HSP/HFP profile, the tap reports 8 kHz. The Swift
helper sets up an ``AVAudioConverter`` from 8 kHz native → 48 kHz target
and writes 48 kHz output. The Python pipeline trusts that and resamples
to 16 kHz.

If the friend's saved capture sounds slowed down at the file's nominal
rate, the most likely cause is a *format mismatch* between what
``kAudioTapPropertyFormat`` declares (the tap's nominal rate) and what
the IO proc actually receives from the aggregate device (which may run
at the system mix rate, e.g. 48 kHz). When that mismatch happens, the
converter produces N× too many output samples and the file plays back N×
slower than reality.

This script bypasses the agent entirely — same ``audio-tap`` binary,
same SAYZ wire format, but written verbatim to WAVs at a *grid* of
sample rates. Whichever WAV sounds right in real time tells us the true
delivery rate.

Usage
-----
::

    python3 scripts/probe_mac_audio_tap_rate.py
    python3 scripts/probe_mac_audio_tap_rate.py --secs 12
    python3 scripts/probe_mac_audio_tap_rate.py --pids 1234,5678
    python3 scripts/probe_mac_audio_tap_rate.py --binary /custom/path/audio-tap

Test procedure
--------------
1. Start playing something through speakers / headset that has a
   distinctive *time signature* — speech with known cadence is best,
   but a 1-bpm metronome / a count-out-loud "1, 2, 3, 4, 5" works
   great. You need something where you can FEEL the timing.

2. Run::

       python3 scripts/probe_mac_audio_tap_rate.py --secs 12

   While it's running, just keep audio playing — it captures the
   system audio for 12 seconds.

3. The script writes ``probe_mac_audio_tap_out/audio_<rate>.wav`` for
   each candidate rate (8000, 16000, 24000, 32000, 44100, 48000) +
   prints the stats it parsed from the SAYZ frames.

4. Play each WAV back (Finder → spacebar preview, or QuickTime). The
   one whose timing matches reality — speech doesn't sound stretched
   or shrunk — is the actual sample rate. Tell us which one it is.

What we expect to see
---------------------
* If 48000 sounds right → the pipeline is fine; the slowdown was
  perceptual and the actual issue is just HSP audio quality.
* If a LOWER rate sounds right (e.g. 8000) → the converter wasn't
  actually upsampling, the data is at the speaker rate verbatim, and
  Swift / the aggregate device lied to us about the format.
  Fix path: hard-pin the IO-proc input format to the aggregate
  device's actual stream config, not what kAudioTapPropertyFormat
  reports.

Also dumps the SAYZ timestamp stream so we can see whether timestamps
themselves disagree with sample counts (a "1 second of timestamps
contains 6 seconds of samples" mismatch confirms the rate bug
mechanically).

Stdlib only — no numpy, no third-party pip installs needed.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path


# Wire protocol constants must mirror sayzo_agent/capture/audio-tap/main.swift.
_MAGIC = b"SAYZ"
_HEADER_SIZE = 16  # 4 magic + 8 Float64 ts + 4 UInt32 byte count

# Default candidate sample rates to write WAVs at — the user listens to
# each and identifies which one matches real-time. This grid covers all
# common speaker/HFP/HSP/A2DP configurations.
_DEFAULT_RATE_GRID = (8_000, 16_000, 24_000, 32_000, 44_100, 48_000)


def _find_audio_tap() -> str:
    """Return the audio-tap binary path. Raises FileNotFoundError otherwise.

    Lookup order (matches sayzo_agent.capture.system_mac):
      1. /Applications/Sayzo.app/.../audio-tap   (production install)
      2. ./sayzo_agent/capture/audio-tap/audio-tap   (repo dev checkout)
      3. ``audio-tap`` on $PATH
    """
    candidates = [
        Path("/Applications/Sayzo.app/Contents/Frameworks/sayzo_agent/capture/audio-tap/audio-tap"),
        Path(__file__).resolve().parent.parent
        / "sayzo_agent" / "capture" / "audio-tap" / "audio-tap",
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    import shutil
    on_path = shutil.which("audio-tap")
    if on_path:
        return on_path
    raise FileNotFoundError(
        "audio-tap not found at any of:\n"
        + "\n".join(f"  {p}" for p in candidates)
        + "\n  (or on $PATH)"
    )


def _f32_to_i16_bytes(floats: bytes) -> bytes:
    """Convert Float32 LE bytes → Int16 LE bytes for WAV output.

    Stdlib only (no numpy). Uses struct on each sample. Slow but fine
    for 12 s of audio (48 kHz × 12 = 576 k samples).
    """
    n = len(floats) // 4
    out = bytearray(n * 2)
    # Process in batches for speed — struct.unpack/pack on large arrays is
    # ~10x faster than per-sample.
    BATCH = 4096
    for i in range(0, n, BATCH):
        chunk_n = min(BATCH, n - i)
        floats_chunk = struct.unpack(
            "<" + ("f" * chunk_n), floats[i * 4 : (i + chunk_n) * 4]
        )
        i16 = []
        for f in floats_chunk:
            v = max(-1.0, min(1.0, f))
            i16.append(int(v * 32767))
        out[i * 2 : (i + chunk_n) * 2] = struct.pack(
            "<" + ("h" * chunk_n), *i16
        )
    return bytes(out)


def _write_wav_at_rate(out_path: Path, pcm_f32: bytes, rate: int) -> None:
    """Write `pcm_f32` (Float32 LE, mono) as a 16-bit WAV claiming `rate`.

    Same byte content, different rate label per file. The byte content
    represents whatever Swift wrote — interpreting it at different
    nominal rates gives us a way to A/B-listen for the true rate.
    """
    pcm_i16 = _f32_to_i16_bytes(pcm_f32)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_i16)


def _read_sayz_stream(stdout_fd, deadline: float) -> tuple[bytes, list[float], list[int]]:
    """Read SAYZ-framed records from stdout until `deadline` (mono time).

    Returns ``(pcm_bytes, timestamps, byte_counts)`` where ``pcm_bytes`` is
    every frame's PCM payload concatenated (Float32 LE), ``timestamps`` is
    each frame's mono-time header value (seconds), and ``byte_counts`` is
    each frame's payload length in bytes. The timestamp + byte-count
    sequences let us cross-check rate (sum(byte_counts)/4 / 48000 vs
    timestamps[-1]-timestamps[0] should match for a correct stream).
    """
    pcm = bytearray()
    timestamps: list[float] = []
    byte_counts: list[int] = []

    frames_read = 0
    while time.monotonic() < deadline:
        # Peek at the header. os.read may return short on EOF.
        header = b""
        while len(header) < _HEADER_SIZE:
            chunk = os.read(stdout_fd, _HEADER_SIZE - len(header))
            if not chunk:
                return bytes(pcm), timestamps, byte_counts
            header += chunk

        magic = bytes(header[:4])
        if magic != _MAGIC:
            print(
                f"ERROR: expected SAYZ magic, got {magic!r} after {frames_read} frames. "
                "The audio-tap binary is stale (pre-protocol-v1) — abandoning probe."
            )
            return bytes(pcm), timestamps, byte_counts

        ts = struct.unpack("<d", bytes(header[4:12]))[0]
        byte_count = struct.unpack("<I", bytes(header[12:16]))[0]
        timestamps.append(ts)
        byte_counts.append(byte_count)

        # Read the PCM payload. Same short-read loop.
        payload = b""
        while len(payload) < byte_count:
            chunk = os.read(stdout_fd, byte_count - len(payload))
            if not chunk:
                return bytes(pcm), timestamps, byte_counts
            payload += chunk
        pcm.extend(payload)
        frames_read += 1

    return bytes(pcm), timestamps, byte_counts


def _summarize(
    pcm: bytes,
    timestamps: list[float],
    byte_counts: list[int],
    declared_rate: int,
) -> None:
    """Print human-readable rate diagnostics."""
    n_samples = len(pcm) // 4  # Float32 LE
    print()
    print("=== captured ===")
    print(f"  frames received      : {len(timestamps)}")
    print(f"  total payload bytes  : {len(pcm)}")
    print(f"  total samples        : {n_samples}")
    print(f"  declared rate        : {declared_rate} Hz (what Swift labels its output)")
    print(
        f"  duration @ declared  : {n_samples / declared_rate:.3f} s  "
        f"(this is what an Opus file at {declared_rate} Hz would play as)"
    )

    if len(timestamps) >= 2:
        ts_span = timestamps[-1] - timestamps[0]
        # Plus the duration of the final block (so we cover the full timeline).
        if byte_counts:
            ts_span += (byte_counts[-1] // 4) / declared_rate
        print(f"  timestamp span       : {ts_span:.3f} s")
        print(
            f"  ratio (sample / ts)  : {n_samples / declared_rate / ts_span:.3f}  "
            f"(should be ~1.000 if the rate is honest)"
        )
        print(
            f"  inferred true rate   : {n_samples / ts_span:.0f} Hz  "
            "(if this is much lower than declared, declared rate is wrong)"
        )

    if pcm:
        # Rough silence check via raw bytes — sum of |float| via struct.
        n_check = min(n_samples, 48_000)  # cap at 1s for speed
        floats = struct.unpack(
            "<" + ("f" * n_check), pcm[: n_check * 4]
        )
        abs_sum = sum(abs(f) for f in floats)
        peak = max(abs(f) for f in floats) if floats else 0.0
        print(
            f"  abs_sum first {n_check}  : {abs_sum:.2f}   "
            f"peak: {peak:.4f}   "
            f"avg: {abs_sum / max(1, n_check):.4f}"
        )
        if abs_sum < 1.0:
            print(
                "  WARNING: captured audio looks silent — make sure something "
                "was actually playing through the speakers."
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--secs", type=float, default=10.0,
        help="Seconds to capture (default: 10).",
    )
    parser.add_argument(
        "--pids", type=str, default="",
        help="Comma-separated target PIDs for per-process scope. "
             "Empty = global tap (recommended for a clean rate test — "
             "you'll have less to debug if the global tap also slows down).",
    )
    parser.add_argument(
        "--binary", type=str, default="",
        help="Override the audio-tap binary path (default: auto-discover).",
    )
    parser.add_argument(
        "--out-dir", type=str, default="./probe_mac_audio_tap_out",
        help="Where to write the WAV-grid + raw stats (default: ./probe_mac_audio_tap_out).",
    )
    parser.add_argument(
        "--rates", type=str, default="",
        help="Comma-separated candidate WAV rates. Default: "
             + ",".join(str(r) for r in _DEFAULT_RATE_GRID),
    )
    parser.add_argument(
        "--declared-rate", type=int, default=48_000,
        help="The rate Swift labels its output as (default: 48000 — current "
             "audio-tap source). Used for the slowdown ratio diagnostic.",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("ERROR: this probe only works on macOS (CoreAudio Process Taps).")
        return 2

    try:
        binary = args.binary or _find_audio_tap()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2

    cmd: list[str] = [binary]
    if args.pids.strip():
        cmd.extend(["--pids", args.pids])

    print(f"audio-tap: {binary}")
    print(f"command  : {' '.join(cmd)}")
    print(f"capturing: {args.secs:.1f}s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rates = (
        tuple(int(r) for r in args.rates.split(",") if r.strip())
        if args.rates
        else _DEFAULT_RATE_GRID
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,  # unbuffered — read SAYZ records as they arrive
    )
    if proc.stdout is None or proc.stderr is None:
        print("ERROR: subprocess pipes not opened")
        return 1

    deadline = time.monotonic() + args.secs
    try:
        pcm, timestamps, byte_counts = _read_sayz_stream(
            proc.stdout.fileno(), deadline
        )
    finally:
        # Clean shutdown — SIGTERM, then SIGKILL after 2 s.
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Capture stderr for the user — every line audio-tap printed
        # ("native NNNN Hz ch=Y" comes through here).
        stderr = proc.stderr.read().decode(errors="replace")

    if stderr:
        print("\n=== audio-tap stderr ===")
        for line in stderr.splitlines():
            print(f"  {line}")

    _summarize(pcm, timestamps, byte_counts, args.declared_rate)

    if not pcm:
        print("\nERROR: no PCM captured. Make sure system audio was actually "
              "playing during the run.")
        return 1

    print()
    print("=== writing rate-grid WAVs ===")
    for rate in rates:
        path = out_dir / f"audio_{rate}.wav"
        _write_wav_at_rate(path, pcm, rate)
        n_samples = len(pcm) // 4
        print(f"  {path.resolve()}  ({n_samples / rate:.2f} s @ {rate} Hz)")

    print()
    print("Now play each WAV file (spacebar in Finder, or QuickTime). The one")
    print("whose timing matches what was actually playing in real-time tells")
    print("us the true sample rate.  Compare against the wall-clock duration")
    print(f"of the capture window: ~{args.secs:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
