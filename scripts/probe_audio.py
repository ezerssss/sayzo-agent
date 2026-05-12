"""Probe Sayzo's production system-audio capture against a user-selected app.

Reuses the same capture class the agent runs in armed sessions
(``ProcessLoopbackCapture`` on Windows, ``SystemCapture`` on macOS), so
"does this probe see audio?" answers "would Sayzo see audio in
production right now?". The discovery shell is a convenience layer —
enumerates the apps currently producing audio output and presents a
numbered menu so you don't have to look up PIDs.

Designed for the Granola-interference / EDR-silent-deny class of
investigation: run the probe once with only Chrome (or whatever)
playing, run it again with the suspected competitor installed +
running, compare the metrics. Same script on both platforms keeps the
methodology identical when you graduate from a Windows repro to a Mac
one.

Usage:
    python scripts/probe_audio.py                       # interactive menu
    python scripts/probe_audio.py --list-only           # just enumerate
    python scripts/probe_audio.py --app chrome          # non-interactive
    python scripts/probe_audio.py --global              # whole-system tap
    python scripts/probe_audio.py --duration 30         # 30 s capture
    python scripts/probe_audio.py --no-wav              # skip WAV dump

Output: per run, a timestamped WAV in ./probe-out/ plus a one-line
metrics summary on stdout (frames, RMS, silence-pct, peak). The WAV
plays back at the agent's pipeline rate (16 kHz mono) so silence vs.
audio is unambiguous on listen.

Run from the project venv:
    .venv\\Scripts\\activate           # Windows
    source .venv/bin/activate          # macOS
    python scripts/probe_audio.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


@dataclass
class AppEntry:
    """One user-facing app's worth of audio activity."""

    label: str                  # display name shown in the menu
    detail: str                 # subtitle with PID / session counts
    pids: tuple[int, ...]       # all PIDs to pass to the capture class
    sort_key: tuple             # for ordering — most-active first
    match_keys: tuple[str, ...] = field(default_factory=tuple)
    # ^ lowercased substrings the user can pass to --app


@dataclass
class CaptureMetrics:
    frames: int
    samples: int
    duration_s: float
    sample_rate: int
    rms: float
    peak: float
    silence_pct: float          # % of frames with abs-max < silence threshold


_SILENCE_THRESH = 0.001         # ~-60 dBFS; below this we call a frame silent


# ----- discovery: Windows ----------------------------------------------------


def _list_windows_audio_apps() -> list[AppEntry]:
    """Enumerate render-endpoint audio sessions and group by .exe name."""
    import psutil
    from comtypes import CLSCTX_ALL, CoCreateInstance
    from pycaw.pycaw import (
        IAudioSessionControl2,
        IAudioSessionManager2,
        IMMDeviceEnumerator,
    )
    from pycaw.constants import (
        CLSID_MMDeviceEnumerator,
        DEVICE_STATE,
        EDataFlow,
    )

    enumerator = CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL,
    )
    devices = enumerator.EnumAudioEndpoints(
        EDataFlow.eRender.value,  # output devices (speakers / headphones)
        DEVICE_STATE.ACTIVE.value,
    )
    n_devices = devices.GetCount()

    # exe-name → {"pids": set[int], "active": int, "inactive": int, "expired": int}
    groups: dict[str, dict] = {}

    for d_idx in range(n_devices):
        device = devices.Item(d_idx)
        try:
            raw = device.Activate(
                IAudioSessionManager2._iid_, CLSCTX_ALL, None,
            )
            mgr = raw.QueryInterface(IAudioSessionManager2)
            session_enum = mgr.GetSessionEnumerator()
            n_sessions = session_enum.GetCount()
        except Exception:
            continue

        for s_idx in range(n_sessions):
            try:
                ctrl = session_enum.GetSession(s_idx)
                ctrl2 = ctrl.QueryInterface(IAudioSessionControl2)
                state = ctrl.GetState()  # 0 Inactive, 1 Active, 2 Expired
                pid = ctrl2.GetProcessId()
            except Exception:
                continue
            if pid <= 0:
                continue
            try:
                proc = psutil.Process(pid)
                exe = (proc.name() or "").lower()
                if not exe:
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            # Skip Windows audio-graph internals — the system mixer that
            # every app feeds into. Picking it would be a no-op.
            if exe in {"audiodg.exe", "system", "system idle process"}:
                continue
            g = groups.setdefault(exe, {
                "pids": set(),
                "active": 0,
                "inactive": 0,
                "expired": 0,
            })
            g["pids"].add(pid)
            if state == 1:
                g["active"] += 1
            elif state == 0:
                g["inactive"] += 1
            else:
                g["expired"] += 1

    entries: list[AppEntry] = []
    for exe, g in groups.items():
        pids = tuple(sorted(g["pids"]))
        n_pids = len(pids)
        active = g["active"]
        inactive = g["inactive"]
        expired = g["expired"]
        nice = _friendly_exe_name(exe)
        detail = (
            f"{n_pids} PID{'s' if n_pids != 1 else ''}, "
            f"{active} active / {inactive} inactive"
            + (f" / {expired} expired" if expired else "")
        )
        entries.append(AppEntry(
            label=nice,
            detail=detail,
            pids=pids,
            # active count primary, total session count secondary
            sort_key=(-active, -(active + inactive), nice.lower()),
            match_keys=(exe.lower().removesuffix(".exe"), nice.lower()),
        ))
    entries.sort(key=lambda e: e.sort_key)
    return entries


_WIN_FRIENDLY_NAMES: dict[str, str] = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "zoom.exe": "Zoom",
    "code.exe": "Visual Studio Code",
    "granola.exe": "Granola",
}


def _friendly_exe_name(exe: str) -> str:
    if exe in _WIN_FRIENDLY_NAMES:
        return _WIN_FRIENDLY_NAMES[exe]
    base = exe.removesuffix(".exe")
    return base[:1].upper() + base[1:] if base else exe


# ----- discovery: macOS ------------------------------------------------------


def _list_mac_audio_apps() -> list[AppEntry]:
    """Use the audio-detect Swift helper to enumerate audio processes."""
    from sayzo_agent.arm import audio_detect

    procs = audio_detect.snapshot(force_refresh=True)

    # Group by bundle id (or process name fallback). Climb to the
    # responsible PID where possible — that's the user-facing app
    # (NSWorkspace-equivalent) rather than a helper.
    groups: dict[str, dict] = {}
    for p in procs:
        if not (p.output or p.input):
            continue
        key = p.bundle_id or f"pid:{p.pid}"
        # Roll helper bundles up to their parent: com.google.Chrome.helper.X
        # → com.google.Chrome. Audio-detect already resolves the responsible
        # PID but the bundle ID can still be a helper's.
        rolled = _roll_up_bundle(key)
        g = groups.setdefault(rolled, {
            "pids": set(),
            "running": 0,
            "input": 0,
            "output": 0,
        })
        target = p.responsible_pid if p.responsible_pid > 0 else p.pid
        g["pids"].add(target)
        if p.running:
            g["running"] += 1
        if p.input:
            g["input"] += 1
        if p.output:
            g["output"] += 1

    entries: list[AppEntry] = []
    for bundle, g in groups.items():
        pids = tuple(sorted(g["pids"]))
        n_pids = len(pids)
        nice = _friendly_bundle_name(bundle)
        detail = (
            f"{n_pids} audio process{'es' if n_pids != 1 else ''}, "
            f"{g['output']} output / {g['input']} input"
        )
        entries.append(AppEntry(
            label=nice,
            detail=detail,
            pids=pids,
            sort_key=(-g["output"], -g["running"], nice.lower()),
            match_keys=(bundle.lower(), nice.lower()),
        ))
    entries.sort(key=lambda e: e.sort_key)
    return entries


_MAC_FRIENDLY_NAMES: dict[str, str] = {
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "com.spotify.client": "Spotify",
    "com.hnc.Discord": "Discord",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.microsoft.teams2": "Microsoft Teams",
    "us.zoom.xos": "Zoom",
    "com.granola.app": "Granola",
    "com.electron.wispr-flow": "Wispr Flow",
    "com.apple.CoreSpeech": "Apple Dictation (CoreSpeech)",
}


def _roll_up_bundle(bundle: str) -> str:
    # Common helper-bundle suffixes — map back to user-facing app.
    helper_suffixes = (
        ".helper.Renderer", ".helper.GPU", ".helper.Plugin",
        ".helper", ".Helper", ".Renderer", ".GPUProcess",
    )
    for suf in helper_suffixes:
        if bundle.endswith(suf):
            return bundle[: -len(suf)]
    return bundle


def _friendly_bundle_name(bundle: str) -> str:
    rolled = _roll_up_bundle(bundle)
    if rolled in _MAC_FRIENDLY_NAMES:
        return _MAC_FRIENDLY_NAMES[rolled]
    return rolled


# ----- interactive menu ------------------------------------------------------


def _print_menu(entries: list[AppEntry]) -> None:
    if not entries:
        print("(no audio sessions found — is anything actually making sound?)")
        return
    print(f"\nAudio sessions currently active ({len(entries)}):")
    width = max(len(e.label) for e in entries)
    for i, e in enumerate(entries, start=1):
        print(f"  [{i:2d}] {e.label:<{width}}  — {e.detail}")
    print()


def _pick_interactive(entries: list[AppEntry]) -> Optional[AppEntry]:
    if not entries:
        return None
    while True:
        try:
            raw = input(f"Pick app to probe [1-{len(entries)}], or 'q' to quit: ").strip()
        except EOFError:
            return None
        if raw.lower() in {"q", "quit", "exit", ""}:
            return None
        try:
            n = int(raw)
        except ValueError:
            print("  (need a number)")
            continue
        if not (1 <= n <= len(entries)):
            print(f"  (out of range — try 1..{len(entries)})")
            continue
        return entries[n - 1]


def _pick_by_substring(entries: list[AppEntry], needle: str) -> Optional[AppEntry]:
    needle = needle.lower().strip()
    # Exact match on any match_key first.
    for e in entries:
        if needle in e.match_keys:
            return e
    # Substring fallback.
    matches = [e for e in entries if any(needle in k for k in e.match_keys)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"  (no audio session matched --app {needle!r})")
        return None
    print(f"  (--app {needle!r} ambiguous — matched: "
          f"{', '.join(m.label for m in matches)})")
    return None


# ----- capture orchestration -------------------------------------------------


async def _drain_and_score(
    capture,
    duration_s: float,
    save_wav: Optional[Path],
) -> CaptureMetrics:
    """Drain frames from ``capture.queue`` for ``duration_s``, score, dump WAV.

    Works for both ``ProcessLoopbackCapture`` (Windows) and ``SystemCapture``
    (macOS) — both publish ``(timestamp, np.ndarray)`` tuples on the queue
    and expose ``sample_rate`` on the instance.
    """
    deadline = time.monotonic() + duration_s
    frames = 0
    samples = 0
    rms_squared_sum = 0.0
    peak = 0.0
    silent_frames = 0
    accum: list[np.ndarray] = []

    while time.monotonic() < deadline:
        try:
            _ts, frame = await asyncio.wait_for(capture.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        frames += 1
        if frame.size == 0:
            continue
        samples += frame.size
        rms_squared_sum += float(np.sum(frame.astype(np.float64) ** 2))
        f_peak = float(np.max(np.abs(frame)))
        peak = max(peak, f_peak)
        if f_peak < _SILENCE_THRESH:
            silent_frames += 1
        if save_wav is not None:
            accum.append(frame)

    rms = (rms_squared_sum / samples) ** 0.5 if samples > 0 else 0.0
    silence_pct = (silent_frames / frames * 100.0) if frames > 0 else 0.0

    if save_wav is not None and accum:
        pcm = np.concatenate(accum)
        pcm_i16 = np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)
        save_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(save_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(capture.sample_rate)
            w.writeframes(pcm_i16.tobytes())

    return CaptureMetrics(
        frames=frames,
        samples=samples,
        duration_s=duration_s,
        sample_rate=capture.sample_rate,
        rms=rms,
        peak=peak,
        silence_pct=silence_pct,
    )


async def _run_capture(
    entry: Optional[AppEntry],
    duration_s: float,
    wav_path: Optional[Path],
) -> CaptureMetrics:
    """Construct the production capture class and drive it.

    ``entry`` ``None`` ⇒ global tap (endpoint-wide loopback on Windows,
    no-PID Process Tap on macOS). This is the same code path Sayzo uses
    when ``system_scope="endpoint"`` — i.e. when per-app scoping is
    disabled. Useful for "does ANY system audio reach us?" testing,
    since global tap bypasses per-process attribution entirely.
    """
    if sys.platform == "win32":
        if entry is not None:
            from sayzo_agent.capture.system_win_process import (
                ProcessLoopbackCapture, is_supported,
            )
            if not is_supported():
                raise RuntimeError(
                    "WASAPI process loopback requires Windows 10 build 19041 "
                    "(version 2004, May 2020) or newer"
                )
            capture = ProcessLoopbackCapture(target_pids=entry.pids)
            await capture.start()
        else:
            from sayzo_agent.capture.system_win import SystemCapture
            capture = SystemCapture(system_scope="endpoint")
            await capture.start()
        try:
            return await _drain_and_score(capture, duration_s, wav_path)
        finally:
            await capture.stop()

    elif sys.platform == "darwin":
        from sayzo_agent.capture.system_mac import SystemCapture
        scope = "arm_app" if entry is not None else "endpoint"
        capture = SystemCapture(system_scope=scope)
        pids = entry.pids if entry is not None else ()
        await capture.start(target_pids=pids)
        try:
            return await _drain_and_score(capture, duration_s, wav_path)
        finally:
            await capture.stop()

    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")


# ----- main ------------------------------------------------------------------


def _list_apps() -> list[AppEntry]:
    if sys.platform == "win32":
        return _list_windows_audio_apps()
    if sys.platform == "darwin":
        return _list_mac_audio_apps()
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def _format_metrics(m: CaptureMetrics) -> str:
    expected_frames = int(m.duration_s * m.sample_rate / 320) or 1  # 20 ms frames
    pct_received = (m.frames / expected_frames * 100.0) if expected_frames else 0.0
    if m.frames == 0:
        verdict = "FAIL: no frames received"
    elif m.silence_pct >= 99.0:
        verdict = "FAIL: stream is silent (>=99% silent frames)"
    elif m.rms < 1e-4:
        verdict = "FAIL: stream RMS near zero"
    elif m.silence_pct >= 50.0:
        verdict = "SUSPECT: >=50% silent frames"
    else:
        verdict = "PASS: real audio captured"
    return (
        f"  frames     = {m.frames}  ({pct_received:.0f}% of expected at "
        f"{m.sample_rate} Hz / 20 ms)\n"
        f"  samples    = {m.samples}\n"
        f"  rms        = {m.rms:.5f}\n"
        f"  peak       = {m.peak:.5f}\n"
        f"  silence    = {m.silence_pct:.1f}% of frames below "
        f"{_SILENCE_THRESH:g}\n"
        f"  {verdict}"
    )


def _build_wav_path(out_dir: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in label.lower())
    return out_dir / f"probe-{safe}-{stamp}.wav"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-40s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


async def _async_main(args: argparse.Namespace) -> int:
    if sys.platform not in {"win32", "darwin"}:
        print(f"ERROR: probe_audio.py only runs on Windows and macOS, not "
              f"{sys.platform!r}", file=sys.stderr)
        return 2

    if args.app and args.global_scope:
        print("ERROR: --app and --global are mutually exclusive.", file=sys.stderr)
        return 2

    # --global skips the discovery menu entirely.
    if args.global_scope:
        out_dir = Path(args.output_dir).resolve()
        wav_path = None if args.no_wav else _build_wav_path(out_dir, "global")
        print(f"\nCapturing GLOBAL system audio (endpoint-wide) for "
              f"{args.duration:.1f}s …")
        if wav_path is not None:
            print(f"  WAV → {wav_path}")
        metrics = await _run_capture(None, args.duration, wav_path)
        print(_format_metrics(metrics))
        return 0

    entries = _list_apps()
    _print_menu(entries)

    if args.list_only:
        return 0

    if args.app:
        entry = _pick_by_substring(entries, args.app)
        if entry is None:
            return 1
        print(f"--app {args.app!r} → {entry.label} (pids={list(entry.pids)})")
    else:
        entry = _pick_interactive(entries)
        if entry is None:
            print("(cancelled)")
            return 0

    out_dir = Path(args.output_dir).resolve()
    wav_path = None if args.no_wav else _build_wav_path(out_dir, entry.label)

    print(f"\nCapturing {entry.label} for {args.duration:.1f}s "
          f"(pids={list(entry.pids)})…")
    if wav_path is not None:
        print(f"  WAV → {wav_path}")

    metrics = await _run_capture(entry, args.duration, wav_path)
    print(_format_metrics(metrics))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Probe Sayzo's production system-audio capture against a "
                    "user-selected app."
    )
    p.add_argument(
        "--duration", type=float, default=15.0,
        help="Seconds to capture (default 15).",
    )
    p.add_argument(
        "--app", type=str, default=None,
        help="Non-interactive: pick the app whose name/exe/bundle contains "
             "this substring (case-insensitive). E.g. 'chrome', 'granola'.",
    )
    p.add_argument(
        "--global", dest="global_scope", action="store_true",
        help="Capture the whole system audio output instead of a single app. "
             "Bypasses the picker. Uses Sayzo's endpoint-scope code path "
             "(WASAPI loopback on default speakers / global Process Tap on "
             "macOS). Useful for 'does ANY audio reach us?' testing — if "
             "global PASSes but --app FAILs, the bug is in per-process "
             "scoping; if global also FAILs, the interference is below "
             "the capture API itself.",
    )
    p.add_argument(
        "--list-only", action="store_true",
        help="Enumerate audio apps and exit. No capture.",
    )
    p.add_argument(
        "--output-dir", type=str, default="./probe-out",
        help="Where to write WAV files (default ./probe-out).",
    )
    p.add_argument(
        "--no-wav", action="store_true",
        help="Skip the WAV dump — just report metrics.",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Enable DEBUG logs from the capture module.",
    )
    args = p.parse_args()

    _setup_logging(args.verbose)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("\n(interrupted)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
