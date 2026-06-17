"""CLI entrypoint for the Sayzo local listening agent."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import typing

import click

from .arm.hotkey import humanize_binding
from .config import load_config

if typing.TYPE_CHECKING:
    # Runtime import is local to the functions that construct it (HudLauncher
    # is heavy); this TYPE_CHECKING import resolves the forward-ref annotation
    # at module scope (e.g. `hud_launcher: "HudLauncher | None"`) for linters
    # and type-checkers without paying the import cost at boot.
    from .gui.hud.launcher import HudLauncher


def _setup_logging(level: str, debug: bool) -> None:
    lvl = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


class _DropHeartbeat(logging.Filter):
    # Heartbeats exist for the `run` terminal view (live "is it alive?" signal).
    # In the 24/7 service log they'd be ~2880 lines/day of low-value chatter and
    # would dominate the file, so filter them out of the file handler only.
    def filter(self, record: logging.LogRecord) -> bool:
        return "[heartbeat]" not in record.getMessage()


def _setup_file_logging(logs_dir, level: str = "INFO", debug: bool = False) -> None:
    """Configure rotating file-based logging for the background service.

    ``level`` / ``debug`` mirror :func:`_setup_logging`. The file root
    is set from the resolved level (not hard-pinned to INFO) so that
    ``SAYZO_LOG_LEVEL=DEBUG`` / ``SAYZO_DEBUG=1`` brings DEBUG lines
    back into ``agent.log`` — important now that high-frequency chatter
    (per-segment echo_guard, HUD geometry) is demoted to DEBUG and is
    otherwise unrecoverable from the file. Default INFO keeps the normal
    24/7 service log unchanged.
    """
    lvl = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    log_file = logs_dir / "agent.log"
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.addFilter(_DropHeartbeat())
    root = logging.getLogger()
    root.setLevel(lvl)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _install_excepthooks(data_dir=None) -> None:
    """Route unhandled exceptions to the file log before the default handler runs.

    Without this, Python's default ``sys.excepthook`` writes the
    traceback to stderr — which is ``/dev/null`` for our windowed
    background exe (no console attached). The user sees a generic
    OS-level "unhandled exception" dialog with no detail, and we get
    no traceback in ``agent.log`` to debug from. Installing a hook
    that calls ``log.critical(..., exc_info=...)`` first ensures
    every crash is captured in the rotating log, so postmortem
    debugging is always possible.

    Hooks installed:
      * ``sys.excepthook`` — main thread synchronous exceptions
      * ``threading.excepthook`` (Python 3.8+) — worker-thread exceptions

    asyncio's per-loop ``set_exception_handler`` is set inside
    ``app.py`` where the loop is constructed; this function only
    handles the cross-thread synchronous paths.

    Idempotent: callers can invoke this from each CLI subcommand's
    setup without checking — re-installing the same hook is a no-op.

    When ``data_dir`` is provided (the production ``service`` path), the
    hooks also drop a tiny crash sentinel under it so the next boot can
    upload ``agent.log`` (gated on ``Config.share_diagnostics``). Dev paths
    (``run`` / ``hud``) pass ``None`` and skip the sentinel.
    """
    log = logging.getLogger("excepthook")

    def _mark_crash() -> None:
        # Best-effort breadcrumb so the next service boot uploads agent.log
        # (gated on Config.share_diagnostics). No-op on the dev run/hud
        # paths (data_dir is None). Never raises — we're already dying.
        if data_dir is None:
            return
        try:
            from .diagnostics import write_crash_sentinel
            write_crash_sentinel(data_dir)
        except Exception:
            pass

    default_sys_excepthook = sys.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb):
        # Don't drown the log on Ctrl+C; it's user intent, not a bug.
        if issubclass(exc_type, KeyboardInterrupt):
            default_sys_excepthook(exc_type, exc_value, exc_tb)
            return
        try:
            log.critical(
                "unhandled exception", exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            pass  # never let the hook itself raise
        _mark_crash()
        default_sys_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        default_thread_excepthook = threading.excepthook

        def _thread_hook(args) -> None:
            if issubclass(args.exc_type, SystemExit):
                default_thread_excepthook(args)
                return
            try:
                log.critical(
                    "unhandled exception in thread %s",
                    args.thread.name if args.thread else "<unknown>",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass
            _mark_crash()
            default_thread_excepthook(args)

        threading.excepthook = _thread_hook


async def _do_login(
    cfg,
    no_browser: bool = False,
    quiet: bool = False,
    *,
    cancel_event: threading.Event | None = None,
    on_url_ready: "typing.Callable[[str], None] | None" = None,
    on_tick: "typing.Callable[[int], None] | None" = None,
) -> None:
    """Run the login flow (PKCE primary, device code fallback).

    The optional keyword callbacks are plumbed straight through to the
    PKCE flow so GUI callers (setup-window bridge, Settings Account pane)
    can surface progress and cancel state. See ``auth/pkce.py`` for the
    semantics.
    """
    from .auth.device import device_code_flow
    from .auth.pkce import pkce_flow
    from .auth.server import HttpAuthServer
    from .auth.store import TokenStore
    from .auth.exceptions import PKCEUnavailable

    server = HttpAuthServer(cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes)
    store = TokenStore(cfg.auth_path, auth_server=server)

    if no_browser:
        tokens = await device_code_flow(server, timeout_secs=cfg.auth.login_timeout_secs)
    else:
        try:
            tokens = await pkce_flow(
                server,
                auth_url=cfg.auth.auth_url,
                client_id=cfg.auth.client_id,
                scopes=cfg.auth.scopes,
                redirect_port=cfg.auth.redirect_port,
                timeout_secs=cfg.auth.login_timeout_secs,
                cancel_event=cancel_event,
                on_url_ready=on_url_ready,
                on_tick=on_tick,
            )
        except PKCEUnavailable:
            if not quiet:
                click.echo("Browser login unavailable, falling back to device code...")
            tokens = await device_code_flow(server, timeout_secs=cfg.auth.login_timeout_secs)

    store.save(tokens)
    if not quiet:
        click.echo("Login successful.")


def _install_agent_side_hud_shutdown_propagation(hud_launcher) -> None:
    """Agent-side belt-and-suspenders for the v2.16.0 HUD shutdown plan.

    Subscribes to OS shutdown signals from the agent process (not the
    HUD subprocess) and pushes ``hud_launcher.quit_sync()`` so the HUD
    starts shutting down even if its own Qt ``commitDataRequest``
    handler is slow to fire. Removes the single-point-of-failure in
    parent → HUD propagation (RC-5 in the plan).

    Both observers are no-ops on the wrong platform — calling either
    on the other OS just returns False without raising.

    Failures are logged at WARNING; the agent continues. The HUD's
    own Qt-side hooks still defend the shutdown invariant if these
    observers don't install.
    """
    from sayzo_agent.gui.common.mac_shutdown import observe_will_power_off
    from sayzo_agent.gui.common.win_shutdown import (
        install_session_ending_callback,
    )

    # Closure-captured logger. This function is module-level (no `log`
    # in scope) and the callback fires later on an OS-shutdown thread,
    # so without this the callback raised `NameError: name 'log' is not
    # defined` the moment the user shut down — silently defeating the
    # whole parent→HUD quit propagation on BOTH macOS and Windows.
    log = logging.getLogger("shutdown")

    def _on_os_shutting_down() -> None:
        log.warning("[agent] OS shutdown signal — pushing quit to HUD subprocess")
        try:
            hud_launcher.quit_sync(timeout_secs=1.0)
        except Exception:
            log.warning(
                "[agent] hud_launcher.quit_sync raised during shutdown",
                exc_info=True,
            )

    # Both helpers internally check sys.platform and silently no-op on
    # the wrong OS, so we can call both unconditionally and let the
    # platform check happen in one place per helper.
    install_session_ending_callback(_on_os_shutting_down)
    observe_will_power_off(_on_os_shutting_down)


def _wait_for_install_lock_release(data_dir, log) -> None:
    """Block boot until any in-flight NSIS installer's File /r completes.

    The Windows NSIS installer (``installer/windows/sayzo-agent.nsi``)
    writes ``<data_dir>/install_in_progress.lock`` at the start of
    Section "Install" and deletes it after the silent-install relaunch
    (v2.8.2+). If we're a fresh ``service`` process spawned during that
    window (user clicked Sayzo from the Start Menu while an auto-update
    was applying), waiting here avoids racing the partial-binary
    replacement — without this, the import chain could hit ImportError
    on a half-replaced ``python3xx.dll`` / ``_internal/*.pyd``.

    Staleness: a crashed/cancelled installer leaves the lock orphan.
    Treat anything older than ``STALE_AGE_SECS`` as dead and proceed —
    better a possibly-racy boot than a permanently-blocked agent. The
    NSIS installer overwrites the lock at the start of every new install
    so the staleness only matters in the crash-recovery edge case.
    """
    lock = data_dir / "install_in_progress.lock"
    if not lock.is_file():
        return

    STALE_AGE_SECS = 300
    POLL_INTERVAL_SECS = 2.0
    MAX_WAIT_SECS = 60

    waited = 0.0
    while waited < MAX_WAIT_SECS:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return  # vanished mid-poll — installer finished
        if age > STALE_AGE_SECS:
            log.warning(
                "[install-lock] stale (%.0fs old) — deleting and proceeding",
                age,
            )
            try:
                lock.unlink()
            except OSError:
                pass
            return
        log.info(
            "[install-lock] install in progress; waiting (%.0fs elapsed)",
            waited,
        )
        time.sleep(POLL_INTERVAL_SECS)
        waited += POLL_INTERVAL_SECS
        if not lock.is_file():
            log.info("[install-lock] released after %.0fs", waited)
            return

    log.warning(
        "[install-lock] still held after %ds — proceeding anyway",
        MAX_WAIT_SECS,
    )


@click.group(invoke_without_command=True)
@click.version_option(package_name="sayzo-agent")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Sayzo local listening agent."""
    # No subcommand → user double-clicked Sayzo.app from /Applications (or
    # ran the bundled exe with no args). LSUIElement=True hides the Dock
    # icon, so without this dispatch click would silently print --help to
    # a non-existent stdout and exit, leaving the user staring at nothing.
    # Defaulting to `service` runs the setup-gate + tray; its kernel-lock
    # check cleanly no-ops when launchd already has the service alive (the
    # primary holds the lock; the secondary's `try_acquire_pidfile` returns
    # False and the secondary asks the primary to open Settings via IPC).
    if ctx.invoked_subcommand is None:
        ctx.invoke(service)


@cli.command(hidden=True)
def devices() -> None:
    """List available mic and loopback devices."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    import sounddevice as sd

    hostapis = sd.query_hostapis()

    def _host_name(host_idx) -> str:
        if host_idx is None or not (0 <= host_idx < len(hostapis)):
            return "?"
        return hostapis[host_idx]["name"]

    click.echo("--- sounddevice (input) ---")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            click.echo(
                f"  [{i}] {d['name']} [{_host_name(d.get('hostapi'))}] "
                f"(in={d['max_input_channels']})"
            )

    if sys.platform == "win32":
        import pyaudiowpatch as pyaudio

        click.echo("\n--- WASAPI loopback devices ---")
        pa = pyaudio.PyAudio()
        try:
            for i in range(pa.get_device_count()):
                dev = pa.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice"):
                    click.echo(f"  [{i}] {dev['name']} (sr={int(dev['defaultSampleRate'])} ch={dev['maxInputChannels']})")
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            click.echo(f"\nDefault output: {default_out['name']}")
        finally:
            pa.terminate()
    elif sys.platform == "darwin":
        click.echo("\n--- macOS system audio ---")
        click.echo("  CoreAudio Process Taps capture all system audio output.")
        click.echo("  No device selection needed (all apps' audio is mixed).")


@cli.command("healthcheck")
def healthcheck() -> None:
    """Exit non-zero if the agent's runtime deps can't actually load.

    Exists because the v3.0.0 build shipped without ``onnxruntime`` —
    ``faster-whisper`` had been the only transitive provider and got
    removed from ``pyproject.toml`` together with the on-device STT
    code; silero-vad declares onnxruntime only as an ``[onnx-cpu]``
    extra, so a clean install left the bundle silent-broken. CI now
    runs this against the built artifact (``dist/sayzo-agent/...``)
    after PyInstaller and before NSIS / DMG packaging, so a regression
    of that shape fails the build instead of the user.

    Checks every runtime that's lazy-loaded enough to evade
    PyInstaller's static analysis. Exits 0 with a summary line per
    component if all pass, exits 1 with a clear "missing X" message
    on the first failure.
    """
    # Imports are done one-by-one so a failure points at exactly the
    # broken dep, not at a wall of stack frames. Click is already
    # imported at module top, so click.echo is safe even if every
    # third-party dep below is broken.
    failures: list[str] = []

    def _try(label: str, fn) -> None:
        try:
            fn()
            click.echo(f"  ok  {label}")
        except Exception as exc:  # noqa: BLE001 — the point IS to catch everything
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            click.echo(f"  FAIL {label}: {type(exc).__name__}: {exc}")

    click.echo("Sayzo agent healthcheck")

    def _check_silero() -> None:
        # The exact load + inference path _consume hits on the first
        # armed frame. Catches a missing onnxruntime native library, a
        # missing/mis-bundled sayzo_agent/data/silero_vad.onnx, and the
        # "imports but can't execute" case — the exact class of break
        # that bit v3.0.0 when onnxruntime silently fell out of the dep
        # graph.
        import numpy as np
        from sayzo_agent.silero_onnx import SileroOnnxModel
        model = SileroOnnxModel()
        prob = model(np.zeros(512, dtype=np.float32), 16000)
        if not (0.0 <= prob <= 1.0):
            raise RuntimeError(f"silero ONNX returned out-of-range prob {prob}")

    def _check_aec() -> None:
        # WebRTC AEC3 via livekit.rtc.apm — ship a synthetic 1 s
        # mic+sys buffer through cancel_echo. Catches the v3.0.0-style
        # "lib imports but native FFI binary didn't make it into the
        # bundle" failure mode for the new dep.
        import numpy as np
        from sayzo_agent.aec import cancel_echo
        from sayzo_agent.config import AecConfig
        sr = 16000
        n = sr
        rng = np.random.default_rng(0)
        mic = (rng.normal(0, 3000, n).astype(np.int16)).tobytes()
        sys_pcm = (rng.normal(0, 3000, n).astype(np.int16)).tobytes()
        out, rep = cancel_echo(mic, sys_pcm, sr, AecConfig(enabled=True))
        if not rep.ran:
            raise RuntimeError(
                f"AEC skipped: {rep.skip_reason} (livekit FFI binary may be missing)"
            )
        if len(out) != len(mic):
            raise RuntimeError(
                f"AEC output length mismatch: {len(out)} vs {len(mic)}"
            )

    _try("numpy import", lambda: __import__("numpy"))
    _try("scipy.signal import", lambda: __import__("scipy.signal"))
    _try("sounddevice import", lambda: __import__("sounddevice"))
    if sys.platform == "win32":
        _try("pyaudiowpatch import", lambda: __import__("pyaudiowpatch"))
        _try("pycaw import", lambda: __import__("pycaw.pycaw"))
        _try("win32gui import", lambda: __import__("win32gui"))
        # Lazy-loaded by arm/platform_win.py for browser-tab URL reads;
        # also guards the spec's Pythonwin/win32ui prune — uiautomation
        # must keep importing without pywin32's MFC payload.
        _try("uiautomation import", lambda: __import__("uiautomation"))
    _try("onnxruntime import", lambda: __import__("onnxruntime"))
    _try("silero VAD load + inference (ONNX)", _check_silero)
    _try("av (PyAV) import", lambda: __import__("av"))
    _try("noisereduce import", lambda: __import__("noisereduce"))
    _try("livekit.rtc.apm import", lambda: __import__("livekit.rtc.apm"))
    _try("AEC end-to-end", _check_aec)
    _try("pydantic import", lambda: __import__("pydantic"))
    _try("httpx import", lambda: __import__("httpx"))
    _try("pystray import", lambda: __import__("pystray"))
    _try("PIL.Image import", lambda: __import__("PIL.Image"))
    _try("pynput import", lambda: __import__("pynput"))
    _try("psutil import", lambda: __import__("psutil"))
    _try("pywebview import", lambda: __import__("webview"))
    _try("PySide6.QtCore import", lambda: __import__("PySide6.QtCore"))
    _try("PySide6.QtWebEngineWidgets import", lambda: __import__("PySide6.QtWebEngineWidgets"))

    if failures:
        click.echo("")
        click.echo(f"healthcheck: {len(failures)} failure(s):")
        for f in failures:
            click.echo(f"  - {f}")
        sys.exit(1)
    click.echo("")
    click.echo("healthcheck: all OK")


@cli.command("test-capture", hidden=True)
@click.option("--seconds", default=10)
@click.option("--dump-wav", is_flag=True, help="Save captured mic/system audio as WAV files for inspection.")
def test_capture(seconds: int, dump_wav: bool) -> None:
    """Capture mic + system audio for N seconds and report frame counts."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    async def _run() -> None:
        from .capture.mic import MicCapture
        from .capture import SystemCapture

        mic = MicCapture(cfg.capture.sample_rate, cfg.capture.frame_ms, cfg.capture.mic_device)
        sys_ = SystemCapture(
            cfg.capture.sample_rate, cfg.capture.frame_ms, cfg.capture.sys_device,
            system_scope=cfg.capture.system_scope,
        )
        await mic.start()
        await sys_.start()
        mic_n = sys_n = 0
        mic_frames = []
        sys_frames = []
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                _ts, frame = await asyncio.wait_for(mic.queue.get(), timeout=0.05)
                mic_n += 1
                if dump_wav:
                    mic_frames.append(frame)
            except asyncio.TimeoutError:
                pass
            try:
                _ts, frame = await asyncio.wait_for(sys_.queue.get(), timeout=0.05)
                sys_n += 1
                if dump_wav:
                    sys_frames.append(frame)
            except asyncio.TimeoutError:
                pass
        await mic.stop()
        await sys_.stop()
        click.echo(f"mic frames: {mic_n}  system frames: {sys_n}")

        if dump_wav and (mic_frames or sys_frames):
            import numpy as np
            from .capture.replay import save_wav
            if mic_frames:
                mic_audio = np.concatenate(mic_frames)
                save_wav(mic_audio, cfg.capture.sample_rate, "test_mic.wav")
            if sys_frames:
                sys_audio = np.concatenate(sys_frames)
                save_wav(sys_audio, cfg.capture.sample_rate, "test_sys.wav")
            click.echo("Wrote test_mic.wav and test_sys.wav -- listen to verify audio quality.")

    asyncio.run(_run())


@cli.command("test-process-loopback", hidden=True)
@click.option("--pid", type=int, required=True, help="Target process PID (and its child process tree).")
@click.option("--seconds", default=10, help="How long to capture before stopping.")
@click.option("--dump-wav", is_flag=True, help="Save captured audio as test_proc_loopback.wav for listening.")
@click.option(
    "--cycles", default=1,
    help="Number of start/stop cycles against the SAME PID inside this one process. "
    "Use --cycles=2 to verify the persistent-thread fix for the 'session 1 OK / "
    "session 2 fails with E_UNEXPECTED' bug.",
)
@click.option("--gap-secs", default=2.0, help="Idle seconds between cycles when --cycles > 1.")
def test_process_loopback(pid: int, seconds: int, dump_wav: bool, cycles: int, gap_secs: float) -> None:
    """Smoke-test the Windows WASAPI Process Loopback capture path.

    Activates ``ProcessLoopbackCapture`` against a single PID, drains its
    queue for ``--seconds`` seconds, then reports frame count + RMS so
    you can verify the COM/ctypes path end-to-end without rebuilding the
    full installer. Use this to iterate on ``system_win_process.py`` --
    it exercises ActivateAudioInterfaceAsync, IAudioClient::Initialize,
    and IAudioCaptureClient::GetBuffer in the exact configuration the
    agent uses, but in <1 s of dev-loop time.

    Example:

        sayzo-agent test-process-loopback --pid 1448 --seconds 5 --dump-wav
    """
    if sys.platform != "win32":
        click.echo("ERROR: WASAPI process loopback is Windows-only.", err=True)
        sys.exit(2)

    cfg = load_config()
    _setup_logging(cfg.log_level, debug=True)

    async def _run() -> None:
        from .capture.system_win_process import ProcessLoopbackCapture, is_supported
        import numpy as np

        if not is_supported():
            click.echo("ERROR: this Windows build is too old (need 10.0.19041+).", err=True)
            sys.exit(2)

        all_audio: list[np.ndarray] = []

        for cycle in range(1, cycles + 1):
            cap = ProcessLoopbackCapture(
                target_pids=(pid,),
                sample_rate=cfg.capture.sample_rate,
                frame_ms=cfg.capture.frame_ms,
            )
            click.echo(
                f"[smoke] cycle {cycle}/{cycles}: starting process-loopback "
                f"pid={pid} for {seconds}s"
            )
            try:
                await cap.start()
            except Exception as exc:
                click.echo(
                    f"ERROR cycle {cycle}: capture.start() raised "
                    f"{type(exc).__name__}: {exc}",
                    err=True,
                )
                sys.exit(1)

            frames: list[np.ndarray] = []
            end = asyncio.get_running_loop().time() + seconds
            n = 0
            while asyncio.get_running_loop().time() < end:
                try:
                    _ts, frame = await asyncio.wait_for(cap.queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                n += 1
                frames.append(frame)

            await cap.stop()

            if not frames:
                click.echo(
                    f"[smoke] cycle {cycle}: FAIL — captured 0 frames in {seconds}s. "
                    "Either COM activation is broken or the target was silent. "
                    "Check the log for the failing call."
                )
                sys.exit(1)

            audio = np.concatenate(frames)
            rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
            peak = float(np.max(np.abs(audio)))
            click.echo(
                f"[smoke] cycle {cycle}: OK — {n} frames, {len(audio)} samples "
                f"({len(audio) / cfg.capture.sample_rate:.2f}s), "
                f"rms={rms:.4f}, peak={peak:.4f}"
            )
            if rms < 1e-5:
                click.echo(
                    f"[smoke] cycle {cycle}: WARN — rms near-zero. "
                    "Capture activated but the stream is silent; "
                    "double-check the target PID is actually playing audio."
                )
            all_audio.append(audio)

            if cycle < cycles:
                click.echo(f"[smoke] sleeping {gap_secs}s before next cycle...")
                await asyncio.sleep(gap_secs)

        if dump_wav and all_audio:
            from .capture.replay import save_wav
            full = np.concatenate(all_audio)
            save_wav(full, cfg.capture.sample_rate, "test_proc_loopback.wav")
            click.echo(
                "[smoke] wrote test_proc_loopback.wav (concatenated all cycles) "
                "— open it in any player to verify."
            )

        if cycles > 1:
            click.echo(
                f"[smoke] all {cycles} cycles passed — re-arming against the "
                "same PID works without E_UNEXPECTED."
            )

    asyncio.run(_run())


@cli.command("test-meeting-ended-flow", hidden=True)
def test_meeting_ended_flow() -> None:
    """Smoke-test the meeting-ended watcher (v2.1.7 changes) end-to-end.

    Drives the real ArmController + ConversationDetector with fake
    capture/VAD/notifier dependencies through two scripted scenarios:

    Scenario A — tab-switch fix:
        Browser holds the mic; foreground tab URL changes to a non-
        whitelisted site (simulating the user tabbing from chatgpt.com
        to news.ycombinator.com mid-voice-session). Expect: NO meeting-
        ended toast fires within 3x the grace window.

    Scenario B — Keep going force-close:
        Browser drops the mic entirely. First meeting-ended toast fires
        and the script clicks "Keep going". Mic stays absent. After
        ``force_close_after_keep_going_secs``, expect a non-interactive
        "Wrapped up your session" toast and a clean disarm.

    Run me before rebuilding the installer to verify the wired-up flow
    works. Each scenario takes ~1 s with the test-tuned timings; total
    runtime under 5 s.
    """
    from typing import Any

    from .arm.controller import ArmController, ArmReason, ArmState
    from .arm.detectors import (
        DetectorSpec, ForegroundInfo, MicHolder,
    )
    from .config import ArmConfig, ConversationConfig, default_detector_specs
    from .conversation import ConversationDetector

    # Inline fakes (kept here so this command works in the installed
    # bundle, which doesn't ship tests/).

    class _FakeCapture:
        def __init__(self) -> None:
            self.start_count = 0
            self.stop_count = 0

        async def start(self, *, target_pids: tuple[int, ...] = ()) -> None:
            self.start_count += 1

        async def stop(self) -> None:
            self.stop_count += 1

        @property
        def is_open(self) -> bool:
            return self.start_count > self.stop_count

    class _FakeVAD:
        def reset(self) -> None:
            pass

    class _FakeNotifier:
        def __init__(self) -> None:
            self.fire_and_forget: list[tuple[str, str]] = []
            self.consent_calls: list[dict[str, Any]] = []
            self.consent_script: list[str] = []

        def notify(self, title: str, body: str) -> None:
            self.fire_and_forget.append((title, body))

        def ask_consent(
            self, title: str, body: str, yes_label: str, no_label: str,
            timeout_secs: float, default_on_timeout: str = "no",
            supersede: bool = False,
        ) -> str:
            del supersede  # HUD-side semantic; ignored under the fake notifier.
            self.consent_calls.append({"title": title, "body": body})
            if self.consent_script:
                return self.consent_script.pop(0)
            return default_on_timeout

    async def _run() -> None:
        # Add a chatgpt-com browser spec on top of defaults so we have
        # something to arm against in scenario A.
        chatgpt_spec = DetectorSpec(
            app_key="chatgpt-com",
            display_name="ChatGPT",
            is_browser=True,
            url_patterns=[r"^https://chatgpt\.com/"],
        )
        cfg = ArmConfig(
            hotkey="ctrl+alt+s",
            poll_interval_secs=0.02,
            consent_toast_timeout_secs=0.05,
            end_toast_timeout_secs=0.05,
            checkin_toast_timeout_secs=0.05,
            meeting_ended_toast_timeout_secs=0.05,
            whitelist_arm_release_grace_secs=0.05,
            force_close_after_keep_going_secs=0.30,
            decline_release_grace_secs=0.05,
            long_meeting_checkin_marks_secs=[3600.0],
            detectors=default_detector_specs() + [chatgpt_spec],
        )
        conv_cfg = ConversationConfig(joint_silence_close_secs=10.0)
        detector = ConversationDetector(conv_cfg)
        notifier = _FakeNotifier()
        mic_cap = _FakeCapture()
        sys_cap = _FakeCapture()
        ctrl = ArmController(
            cfg, detector,
            mic_capture=mic_cap, sys_capture=sys_cap,
            vad_mic=_FakeVAD(), vad_sys=_FakeVAD(),
            notifier=notifier,
            get_mic_holders=lambda: [MicHolder("chrome.exe", 9999)],
            get_foreground_info=lambda: ForegroundInfo(
                process_name="chrome.exe", is_browser=True,
                browser_tab_url="https://chatgpt.com/c/abc",
            ),
            resolve_pids_for_spec=lambda spec: (9999,),
        )
        ctrl._loop = asyncio.get_running_loop()

        # Force into ARMED with a chatgpt-com reason and start the
        # meeting-ended watcher manually — we don't want the consent
        # toast / whitelist arm path here, just the watcher under test.
        reason = ArmReason(
            source="whitelist", display_name="ChatGPT",
            app_key="chatgpt-com", target_pids=(9999,),
        )
        await ctrl._arm_internal(reason)

        # ---- Scenario A: tab-switch must NOT trip meeting-ended -----
        click.echo("[smoke] scenario A: user tabs away from chatgpt.com to a "
                   "non-whitelisted site (browser still holds mic)")
        ctrl._q_foreground = lambda: ForegroundInfo(
            process_name="chrome.exe", is_browser=True,
            browser_tab_url="https://news.ycombinator.com/",
            browser_window_urls=("https://news.ycombinator.com/",),
        )
        # Wait three grace windows. Old build would have toasted within
        # one grace window; new build must not toast at all.
        await asyncio.sleep(cfg.whitelist_arm_release_grace_secs * 3)
        ended_toasts = [c for c in notifier.consent_calls
                        if c["title"].startswith("Looks like your meeting")]
        if ended_toasts:
            click.echo(
                f"[smoke] scenario A: FAIL — meeting-ended toast fired "
                f"{len(ended_toasts)} time(s) during tab switch. "
                "URL re-check fix is broken."
            )
            sys.exit(1)
        if ctrl.state != ArmState.ARMED:
            click.echo(
                f"[smoke] scenario A: FAIL — agent disarmed during "
                f"tab switch (state={ctrl.state}). Should stay ARMED."
            )
            sys.exit(1)
        click.echo("[smoke] scenario A: OK — agent stayed ARMED through "
                   "tab switch, no false toast.")

        # ---- Scenario B: Keep going then sustained absence ----------
        click.echo("[smoke] scenario B: browser releases mic; user clicks "
                   "'Keep going' on the first toast; absence persists")
        # Script the consent response: "no" = Keep going.
        notifier.consent_script = ["no"]
        # Drop chrome from mic-holders entirely.
        ctrl._q_mic_holders = lambda: []
        ctrl._q_foreground = lambda: ForegroundInfo(
            process_name="chrome.exe", is_browser=True,
            browser_tab_url="https://chatgpt.com/c/abc",
        )
        # First toast should fire after grace; user clicks Keep going;
        # then we need to wait force_close_after_keep_going_secs more
        # for the silent close.
        timeout = (
            cfg.whitelist_arm_release_grace_secs
            + cfg.force_close_after_keep_going_secs
            + 0.4   # safety margin for poll cadence
        )
        await asyncio.sleep(timeout)
        if ctrl.state != ArmState.DISARMED:
            click.echo(
                f"[smoke] scenario B: FAIL — agent should be DISARMED "
                f"after keep-going + sustained absence, got {ctrl.state}"
            )
            sys.exit(1)
        # Exactly one CONSENT toast (the first) fired.
        ended_toasts = [c for c in notifier.consent_calls
                        if c["title"].startswith("Looks like your meeting")]
        if len(ended_toasts) != 1:
            click.echo(
                f"[smoke] scenario B: FAIL — expected exactly 1 consent "
                f"toast, got {len(ended_toasts)}. Old snooze-and-refire "
                f"behavior may have leaked back in."
            )
            sys.exit(1)
        # Informational fire-and-forget toast fired.
        info_toasts = [t for t in notifier.fire_and_forget
                       if "Wrapped up" in t[0]]
        if len(info_toasts) != 1:
            click.echo(
                f"[smoke] scenario B: FAIL — expected one informational "
                f"'Wrapped up' toast, got {[t[0] for t in notifier.fire_and_forget]}"
            )
            sys.exit(1)
        click.echo("[smoke] scenario B: OK — silent force-close fired "
                   "after Keep going; informational toast shown; agent disarmed.")
        click.echo("[smoke] all meeting-ended-flow scenarios passed.")

    asyncio.run(_run())


@cli.command(hidden=True)
@click.argument("audio_file", type=click.Path(exists=True))
@click.option("--speed", default=1.0, help="Playback speed multiplier. 0 = as fast as possible.")
@click.option(
    "--channel",
    type=click.Choice(["both", "mic", "system"]),
    default="both",
    help="Where to route mono audio. 'both' sends to mic+system (default). "
    "'mic' sends to mic only (system gets silence). "
    "'system' sends to system only (mic gets silence). "
    "Ignored for stereo files (L=mic, R=system).",
)
@click.option("--dump-wav", is_flag=True, help="Save loaded mic/sys channels as WAV files for inspection, then exit.")
def replay(audio_file: str, speed: float, channel: str, dump_wav: bool) -> None:
    """Replay a recorded audio file through the full pipeline.

    AUDIO_FILE can be WAV, MP3, OGG, OPUS, or any format PyAV supports.
    Stereo files: left channel = mic, right channel = system audio.
    Mono files: routed by --channel (default: both).

    Examples:\b
        sayzo-agent replay conversation.wav
        sayzo-agent replay conversation.wav --speed 4
        sayzo-agent replay conversation.wav --channel mic
        sayzo-agent replay call.mp3 --channel system --speed 0
    """
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("replay")

    from .app import Agent
    from .capture.replay import ReplayCapture, load_audio

    mic_audio, sys_audio = load_audio(
        audio_file, target_sr=cfg.capture.sample_rate, channel=channel,
    )
    if mic_audio.size == 0 and sys_audio.size == 0:
        log.error("No audio data found in %s", audio_file)
        sys.exit(1)

    if dump_wav:
        from .capture.replay import save_wav
        save_wav(mic_audio, cfg.capture.sample_rate, "replay_mic.wav")
        save_wav(sys_audio, cfg.capture.sample_rate, "replay_sys.wav")
        log.info("Wrote replay_mic.wav and replay_sys.wav — inspect and play these to verify.")
        return

    mic_cap = ReplayCapture(
        mic_audio,
        sample_rate=cfg.capture.sample_rate,
        frame_ms=cfg.capture.frame_ms,
        speed=speed,
    )
    sys_cap = ReplayCapture(
        sys_audio,
        sample_rate=cfg.capture.sample_rate,
        frame_ms=cfg.capture.frame_ms,
        speed=speed,
    )

    agent = Agent(cfg, mic_capture=mic_cap, sys_capture=sys_cap)

    async def _main() -> None:
        await agent.run()

    # When both replay captures finish, wait for any open session to close
    # via silence timeout, then stop the agent.
    async def _watch_done() -> None:
        await mic_cap.done.wait()
        await sys_cap.done.wait()
        log.info("replay audio exhausted — waiting for queues to drain...")

        # Wait for _consume tasks to finish processing all queued frames.
        while not mic_cap.queue.empty() or not sys_cap.queue.empty():
            await asyncio.sleep(0.1)

        # Flush VADs — if audio ended mid-speech, the VAD holds an
        # in-progress segment that won't close without more silence.
        now = time.monotonic()
        for seg in agent.vad_mic.flush():
            agent.detector.on_segment(seg, now)
        for seg in agent.vad_sys.flush():
            agent.detector.on_segment(seg, now)

        # Now wait for the detector to close any open session via silence
        # timeout. Track whether a session ever opened so we don't wait
        # the full timeout for nothing.
        session_was_open = agent.detector.state.value == "open"
        if session_was_open:
            log.info("session is open — waiting for silence timeout to close it...")
            timeout = cfg.conversation.joint_silence_close_secs + 10
            for _ in range(int(timeout)):
                await asyncio.sleep(1.0)
                if agent.detector.state.value == "idle":
                    break
        else:
            # Give the ticker a couple seconds to pick up any just-closed session.
            await asyncio.sleep(3.0)

        # Wait for all in-flight processing tasks (DSP, sink, upload) to finish.
        if agent._processing_tasks:
            log.info("waiting for %d processing task(s)...", len(agent._processing_tasks))
            await asyncio.gather(*agent._processing_tasks, return_exceptions=True)
        log.info("replay complete — shutting down")
        agent.stop()

    async def _run_all() -> None:
        asyncio.create_task(_watch_done())
        await _main()

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option("--no-browser", is_flag=True, help="Use device code flow instead of browser redirect.")
def login(no_browser: bool) -> None:
    """Authenticate with the Sayzo server."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    if not cfg.auth.auth_url or not cfg.auth.client_id:
        click.echo("Auth not configured. Set SAYZO_AUTH__AUTH_URL and SAYZO_AUTH__CLIENT_ID.")
        sys.exit(1)

    asyncio.run(_do_login(cfg, no_browser))


@cli.command()
def logout() -> None:
    """Remove stored credentials."""
    cfg = load_config()
    from .auth.store import TokenStore

    store = TokenStore(cfg.auth_path)
    store.clear()
    click.echo("Logged out.")


@cli.command("diagnose-notifications")
def diagnose_notifications() -> None:
    """Run a HUD-notification diagnostic and dump the report.

    Spins up a temporary :class:`HudLauncher`, waits for the HUD
    subprocess to emit ``hud_ready``, then fires one fire-and-forget
    toast and one consent card. Prints a structured report to stdout
    AND writes every step into ``~/.sayzo/agent/logs/agent.log`` so the
    same data is available offline if the user pastes the file later.

    Use this when notifications appear to do nothing on a user's
    machine — the report + log lines pinpoint whether the failure is in
    the HUD subprocess (never reached ``hud_ready``), the JSON pipe
    (commands sent but no response), or the React app (rendered but no
    user interaction round-trip).
    """
    import json as _json

    cfg = load_config()
    _setup_file_logging(cfg.logs_dir, cfg.log_level, cfg.debug)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s  %(message)s")
    )
    logging.getLogger().addHandler(stream_handler)

    from .gui.hud.launcher import HudLauncher
    from .notify import HudNotifier

    async def _run_diag() -> dict:
        launcher = HudLauncher()
        await launcher.start()
        ready = await launcher.wait_for_ready(timeout_secs=20.0)
        report: dict = {
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "hud_ready": ready,
            "diagnostic": launcher.diagnose(),
            "test_toast": None,
            "consent_round_trip": None,
        }
        if not ready:
            await launcher.quit()
            return report

        notifier = HudNotifier(launcher)
        try:
            notifier.notify(
                "Sayzo notification test",
                "If you can read this, the HUD is wired correctly.",
            )
            report["test_toast"] = {"ok": True}
        except Exception as exc:
            report["test_toast"] = {"ok": False, "error": repr(exc)}

        # Give the toast a moment to render before stacking the card.
        await asyncio.sleep(1.0)

        # ask_consent is sync — run on the executor so we don't block
        # the loop and starve the stdout reader resolving the response.
        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(
                None,
                lambda: notifier.ask_consent(
                    "Sayzo consent test",
                    "If you see Yes/No buttons, consent prompts work. "
                    "Click either — this is just a round-trip check.",
                    "Yes",
                    "No",
                    8.0,
                    "timeout",
                ),
            )
            report["consent_round_trip"] = {"ok": True, "result": answer}
        except Exception as exc:
            report["consent_round_trip"] = {"ok": False, "error": repr(exc)}

        await launcher.quit()
        return report

    click.echo("=== Sayzo HUD diagnostic ===\n")
    click.echo("Spawning HUD subprocess and exercising the round-trip...\n")
    report = asyncio.run(_run_diag())

    click.echo("\n=== Report ===")
    click.echo(_json.dumps(report, indent=2, default=str))
    click.echo("\nFull trace written to: " + str(cfg.logs_dir / "agent.log"))
    click.echo(
        "\nNo HUD on screen? Check:\n"
        "  • Look for `[hud] spawning subprocess` and `[hud] subprocess "
        "emitted hud_ready` in agent.log — these are the two key "
        "lifecycle lines.\n"
        "  • A missing `hud_ready` means the React app didn't mount — "
        "verify the webui bundle exists at "
        "`sayzo_agent/gui/webui/dist/index.html` (run `npm run build` "
        "from `sayzo_agent/gui/webui/` if not).\n"
        "  • A `[hud] giving up after N respawns` line means the "
        "subprocess crashed repeatedly — the underlying error is in "
        "the lines just before, usually a webview backend init failure.\n"
    )


def _mac_login_item_active() -> bool:
    """Will launchd start Sayzo at next login on this Mac?

    Replaces the pre-v2.7.0 ``mac_plist.exists()`` check that sniffed
    ``~/Library/LaunchAgents/com.sayzo.agent.plist`` directly. With
    SMAppService.agent the plist lives inside the .app bundle and the
    registration is in the BTM database, not on disk in the user's home
    — so the existence check has to go through the SMAppService API.
    """
    try:
        from .gui.setup.launchd import is_registered

        return is_registered()
    except Exception:
        return False


@cli.command("first-run")
@click.pass_context
def first_run(ctx: click.Context) -> None:
    """One-time setup: log in and start the service."""
    from rich.console import Console

    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    console = Console()

    # Let Ctrl+C kill the process immediately.
    ctx.resilient_parsing = True
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))

    console.print()
    console.print("[bold cyan]  Sayzo Setup[/]")
    console.print("[cyan]  ===========[/]")
    console.print()

    # Step 1: Login
    from .auth.store import TokenStore

    store = TokenStore(cfg.auth_path)
    if store.has_tokens():
        console.print("  [green]Already logged in.[/]")
    elif cfg.auth.auth_url and cfg.auth.client_id:
        console.print("  Your browser will open to log in to Sayzo.")
        console.print()
        for i in range(3, 0, -1):
            console.print(f"  Opening browser in [bold]{i}[/]...", end="\r")
            time.sleep(1)
        console.print("  Opening browser...           ")
        console.print()

        # Suppress noisy HTTP/auth logs during login.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("sayzo_agent.auth").setLevel(logging.WARNING)

        try:
            asyncio.run(_do_login(cfg, quiet=True))
            console.print("  [green]Login successful.[/]")
        except KeyboardInterrupt:
            console.print("\n  [yellow]Cancelled.[/]")
            sys.exit(130)
        except Exception as e:
            console.print(f"  [yellow]Login skipped: {e}[/]")
            console.print("  You can log in later with: [bold]sayzo-agent login[/]")
    else:
        console.print("  [dim]Auth not configured — skipping login.[/]")

    # Step 2: Start the service in the background (if not already running)
    from pathlib import Path

    from .pidfile import is_running

    console.print()
    if is_running(cfg.pid_path):
        console.print("  [green]Sayzo is already running.[/]")
    elif sys.platform == "darwin" and _mac_login_item_active():
        # launchd owns the service on installed macOS via SMAppService; it
        # will start Sayzo at the user's next login. Spawning our own
        # subprocess here would race it for the pidfile and leak the
        # detached service's stderr to this terminal.
        console.print("  [green]Sayzo is configured to start automatically.[/]")
    else:
        console.print("  Starting Sayzo...")
        import subprocess
        exe = sys.executable
        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Detach from the installer shell so closing Terminal doesn't SIGHUP the service.
            popen_kwargs["start_new_session"] = True
        if getattr(sys, "frozen", False):
            # On Windows, prefer the sibling windowless service exe so no
            # console window appears in the background.
            if sys.platform == "win32":
                service_exe = Path(exe).parent / "sayzo-agent-service.exe"
                if service_exe.exists():
                    exe = str(service_exe)
            subprocess.Popen([exe, "service"], **popen_kwargs)
        else:
            subprocess.Popen([exe, "-m", "sayzo_agent", "service"], **popen_kwargs)
        console.print("  [green]Sayzo is now running in the background.[/]")

    console.print()
    console.print("  [bold green]Setup complete![/]")
    console.print("  The agent will start automatically on login.")
    console.print()


@cli.command()
@click.option("-n", "--lines", default=50, help="Number of lines to show initially.")
@click.option("-f", "--follow", is_flag=True, default=True, help="Stream new lines as they appear (default).")
@click.option("--no-follow", is_flag=True, help="Print last N lines and exit.")
def logs(lines: int, follow: bool, no_follow: bool) -> None:
    """Tail the agent log file (like tail -f)."""
    cfg = load_config()
    log_file = cfg.logs_dir / "agent.log"
    if not log_file.exists():
        click.echo(f"No log file found at {log_file}")
        click.echo("The agent service hasn't run yet, or logs were cleared.")
        raise SystemExit(1)

    # Print last N lines
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        for line in tail:
            click.echo(line, nl=False)

    if no_follow:
        return

    # Stream new lines
    click.echo(f"\n--- following {log_file} (Ctrl+C to stop) ---\n")
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    click.echo(line, nl=False)
                else:
                    # Check if file was rotated (size shrank)
                    try:
                        if f.tell() > log_file.stat().st_size:
                            f.seek(0)
                    except OSError:
                        pass
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass


@cli.command()
def run() -> None:
    """Run the listening agent (foreground, verbose terminal output)."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    _install_excepthooks()
    from .comtypes_setup import configure_comtypes_cache
    configure_comtypes_cache(cfg.data_dir / "comtypes_cache")
    log = logging.getLogger("run")

    # Single-instance enforcement, same kernel-lock gate as ``service``.
    # Without this, a developer iterating in two terminals (or the dev
    # ``run`` while an installed ``service`` is also active) ends up with
    # two primaries both holding the audio devices — the failure mode
    # the user explicitly called out. ``run`` doesn't host an IPC server,
    # so a second invocation silently exits without trying to surface
    # Settings (the only signal the user gets is the log line below).
    from .pidfile import try_acquire_pidfile, remove_pid

    if not try_acquire_pidfile(cfg.pid_path):
        log.warning(
            "agent already running (pidfile=%s) — exiting", cfg.pid_path,
        )
        return

    from .auth.store import TokenStore

    upload_client = None
    auth_client = None
    store = TokenStore(cfg.auth_path)
    if not store.has_tokens():
        log.warning("Not authenticated. Run `sayzo-agent login` to enable uploads.")
    elif cfg.auth.effective_server_url:
        from .auth.client import AuthenticatedClient
        from .auth.server import HttpAuthServer
        from .upload import AuthenticatedUploadClient

        auth_server = HttpAuthServer(cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes)
        store = TokenStore(cfg.auth_path, auth_server=auth_server)
        auth_client = AuthenticatedClient(cfg.auth.effective_server_url, store)
        upload_client = AuthenticatedUploadClient(auth_client, cfg.captures_dir)
        log.info("Uploads enabled → %s", cfg.auth.effective_server_url)

    from .app import Agent
    from .gui.hud.launcher import HudLauncher
    from .notify import HudNotifier, NoopNotifier

    hud_launcher: HudLauncher | None = (
        HudLauncher() if cfg.notifications_enabled else None
    )
    notifier = (
        HudNotifier(hud_launcher) if hud_launcher is not None else NoopNotifier()
    )
    agent = Agent(cfg, upload_client=upload_client, notifier=notifier, auth_client=auth_client)

    async def _main() -> None:
        loop = asyncio.get_running_loop()

        def _handle_stop() -> None:
            log.info("shutdown requested")
            agent.stop()

        try:
            loop.add_signal_handler(signal.SIGINT, _handle_stop)
            loop.add_signal_handler(signal.SIGTERM, _handle_stop)
        except NotImplementedError:
            # Windows: signal handlers via add_signal_handler are unsupported
            pass

        if hud_launcher is not None:
            await hud_launcher.start()
            _install_agent_side_hud_shutdown_propagation(hud_launcher)

        try:
            await agent.run()
        finally:
            if hud_launcher is not None:
                await hud_launcher.quit()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        remove_pid(cfg.pid_path)


@cli.command()
@click.option(
    "--force-setup",
    is_flag=True,
    hidden=True,
    help="Always open the first-run GUI even if setup looks complete. "
    "Passed by every install path for visual post-install confirmation.",
)
@click.option(
    "--from-autostart",
    is_flag=True,
    hidden=True,
    help="Set by the Windows HKCU Run-key auto-start path so the agent "
    "suppresses the user-click Settings auto-open. Without this, every "
    "login would auto-pop Settings — explorer.exe (the Run-key host) is "
    "treated as a user shell by looks_user_launched().",
)
@click.option(
    "--open-settings",
    is_flag=True,
    hidden=True,
    help="Auto-open the Settings window on boot. Set by the NSIS silent-"
    "install relaunch path so a user who clicked 'Install update' from "
    "Settings lands back in Settings on the new version. Bypasses the "
    "looks_user_launched() heuristic, which doesn't fire when the parent "
    "process is the installer (Exec'd from NSIS) rather than explorer.exe.",
)
def service(force_setup: bool, from_autostart: bool, open_settings: bool) -> None:
    """Run the agent as a background service (no terminal output, file logging)."""
    cfg = load_config()
    _setup_file_logging(cfg.logs_dir, cfg.log_level, cfg.debug)
    # Pass data_dir so the excepthook drops a crash sentinel that the boot
    # sweep uploads next time (production service path only).
    _install_excepthooks(cfg.data_dir)
    # Redirect comtypes runtime cache off %TEMP% before any pycaw /
    # uiautomation import. CI pre-bakes the common typelibs into the
    # frozen bundle (see scripts/prebake_comtypes.py); this is defense
    # in depth for unforeseen typelibs.
    from .comtypes_setup import configure_comtypes_cache
    configure_comtypes_cache(cfg.data_dir / "comtypes_cache")
    log = logging.getLogger("service")

    from .pidfile import try_acquire_pidfile, remove_pid

    # Kernel-level single-instance gate. ``try_acquire_pidfile`` uses
    # a Windows named mutex / Unix flock — both auto-release on process
    # death (clean exit, kill, BSOD, reboot), so a stale .pid file from
    # a previous session is harmless. The loser asks the winner to
    # surface Settings via IPC, then exits. ``call_quiet`` swallows
    # IPCNotConnected (winner's IPC server may not be up yet in the
    # rare just-started case), so a mid-startup primary degrades to
    # the prior silent-exit behavior.
    if not try_acquire_pidfile(cfg.pid_path):
        try:
            from .gui.settings.ipc import IPCClient, Methods

            IPCClient(cfg.data_dir).call_quiet(Methods.OPEN_SETTINGS)
            log.warning(
                "service already running — asked primary to open Settings, exiting"
            )
        except Exception:
            log.warning(
                "service already running — IPC handoff failed, exiting",
                exc_info=True,
            )
        return

    # Install-progress race guard (v2.8.2). If an NSIS installer is
    # currently doing File /r on this bundle, racing it with a partial-
    # binary boot risks ImportError mid-load on python3xx.dll or one of
    # the _internal/ DLLs. Block here until the installer releases its
    # lock file. Stale lock (crashed installer) > 5 min is treated as
    # dead and overwritten. Must run before any heavy import that could
    # be mid-replace.
    _wait_for_install_lock_release(cfg.data_dir, log)

    from . import __version__
    log.warning("sayzo-agent service starting v%s (pid=%d)", __version__, os.getpid())

    # Auto-update boot-time apply. Handles the "user rebooted before
    # quitting" case: launchd / Task Scheduler boots the OLD agent that
    # was on disk before the swap, we detect a newer staged version, and
    # hand off to the installer / swap helper before any heavy work
    # starts. ``apply_staged_if_newer`` only returns when there's nothing
    # to apply (or spawn raised) — on success the helper calls
    # ``os._exit`` inside and we never reach the rest of ``service()``.
    # The kernel pidfile lock released by os._exit lets the new agent
    # acquire it cleanly when it relaunches.
    #
    # Clear any stale quit-apply intent so a crashed prior session can't
    # silently auto-install on the NEXT plain tray Quit.
    from .update_apply import (
        apply_staged_if_newer,
        clear_quit_apply_intent,
    )
    clear_quit_apply_intent(cfg.data_dir)
    apply_staged_if_newer(cfg.data_dir, __version__, where="boot")

    # Post-upgrade detection. ``last_seen_version.txt`` is the
    # source-of-truth for "what we ran last time"; if it's strictly older
    # than ``__version__`` we just successfully applied an auto-update —
    # clear the now-obsolete stage and queue the "Sayzo updated" toast for
    # after the notifier is constructed downstream (see _build_pipeline_state).
    from .last_version import read_last_seen, write_last_seen
    from .update import is_newer as _update_is_newer
    from .update_apply import clear_apply_attempts, get_failed_apply_version
    from .update_stage import clear_staged

    _prior_version = read_last_seen(cfg.data_dir)
    _pending_upgrade_toast: typing.Optional[tuple[str, str]] = None
    # True only on the boot that crosses from a pre-3.16.0 version into
    # 3.16.0+, where opt-out diagnostics turn ON for existing users who never
    # re-run onboarding. The post-upgrade toast below uses this to disclose
    # diagnostics proactively — the only proactive surface an upgrader gets,
    # and load-bearing for the opt-out ethics. ``last_seen_version`` is
    # rewritten every boot, so the crossing condition is naturally once-only.
    _diagnostics_disclosure_due = False
    if _prior_version is not None and _update_is_newer(_prior_version, __version__):
        log.warning(
            "[update] post-upgrade detected: prior=v%s now=v%s",
            _prior_version, __version__,
        )
        _pending_upgrade_toast = (_prior_version, __version__)
        _diagnostics_disclosure_due = _update_is_newer(_prior_version, "3.16.0")
        clear_staged(cfg.data_dir)
        # Apply succeeded — wipe any leftover attempts marker from the
        # version we just upgraded TO so a future apply-fail toast can't
        # fire stale.
        clear_apply_attempts(cfg.data_dir)
    write_last_seen(cfg.data_dir, __version__)

    # Apply-failed toast: if a previous boot exhausted MAX_APPLY_ATTEMPTS on
    # a staged version that's still newer than what we're running, surface a
    # toast so the user knows to download the build manually instead of
    # waiting for the in-app updater that's been silently looping. Consume
    # the marker so the toast only fires once per failure episode.
    _pending_apply_failed_toast: typing.Optional[str] = None
    _failed_apply_version = get_failed_apply_version(cfg.data_dir)
    if (_failed_apply_version is not None
            and _update_is_newer(__version__, _failed_apply_version)):
        log.warning(
            "[update] previous apply attempts for v%s exhausted — queueing "
            "user-visible failure toast", _failed_apply_version,
        )
        _pending_apply_failed_toast = _failed_apply_version
    clear_apply_attempts(cfg.data_dir)

    # macOS bundle self-heal: strip the com.apple.quarantine xattr and,
    # for unsigned dev builds only, ad-hoc-sign the Swift helpers so
    # they can spawn under Apple Silicon's mandatory-signature loader.
    # Production builds are Developer-ID-signed by `codesign --deep`
    # in CI; the heal step verifies each helper and skips the ad-hoc
    # resign when the existing signature is valid (replacing a
    # Developer-ID sig with ad-hoc would break notarization). No-op on
    # Linux / Windows and on dev (non-frozen) runs. Must run before the
    # first-run gate, which spawns audio-tap.
    if sys.platform == "darwin":
        from .macos_bundle_heal import heal_bundle
        heal_bundle()

        # SMAppService migration (v2.7.0+). On a v2.6.x -> v2.7.0 upgrade,
        # the user has already completed first-run, so the post-setup
        # call below would never fire. Doing it here on every macOS
        # service start handles the upgrade migration: the helper deletes
        # any legacy ``~/Library/LaunchAgents/com.sayzo.agent.plist`` and
        # registers the bundle plist via SMAppService so the BTM
        # "running in the background" notification + System Settings
        # entry are attributed to "Sayzo" instead of the Developer-ID
        # team identity. Idempotent — register() on an already-registered
        # agent is a no-op and will NOT re-fire the BTM toast.
        try:
            from .gui.setup.launchd import ensure_launchd_registered

            ensure_launchd_registered()
        except Exception:
            log.warning(
                "SMAppService registration at service start failed (non-fatal)",
                exc_info=True,
            )

    # First-run gate. Open the GUI setup window when setup signals are
    # missing, when --force-setup is passed (every install path does), or
    # on the first .app launch on macOS. Blocks the main thread; cancel
    # exits cleanly without starting the tray + agent.
    from .gui.setup.detect import detect_setup
    from .gui.setup.marker import is_first_launch, mark_setup_seen

    setup_status = detect_setup(cfg)
    mac_first_launch = sys.platform == "darwin" and is_first_launch(cfg)
    should_show_gui = (
        force_setup or mac_first_launch or not setup_status.is_complete
    )
    log.warning(
        "first-run gate: force_setup=%s mac_first_launch=%s is_complete=%s "
        "(token=%s mic=%s onboarded=%s) → show_gui=%s",
        force_setup,
        mac_first_launch,
        setup_status.is_complete,
        setup_status.has_token,
        setup_status.has_mic_permission,
        setup_status.has_permissions_onboarded,
        should_show_gui,
    )

    if should_show_gui:
        from .gui.setup.window import SetupWindow
        from .gui.setup.bridge import SetupResult

        try:
            setup_result = SetupWindow(cfg).run_blocking()
        except Exception:
            log.exception("setup window crashed — exiting")
            remove_pid(cfg.pid_path)
            return
        if setup_result == SetupResult.QUIT:
            log.warning("user cancelled setup — exiting")
            remove_pid(cfg.pid_path)
            # Force-exit. pywebview's Cocoa backend doesn't always tear down
            # NSApp cleanly when the only window is destroyed mid-flow (e.g.,
            # a worker thread still has an evaluate_js call queued against the
            # dying window), leaving a non-responsive process behind that the
            # user has to force-quit. The setup window owns no agent / capture
            # state worth flushing, so a hard exit here is safe and reliable.
            os._exit(0)
        # COMPLETED: persist the first-launch marker so subsequent launchd /
        # Task Scheduler restarts don't re-open the GUI unnecessarily, then
        # register the macOS launchd LaunchAgent for auto-start on login.
        mark_setup_seen(cfg)
        if sys.platform == "darwin":
            try:
                from .gui.setup.launchd import ensure_launchd_registered

                ensure_launchd_registered()
            except Exception:
                log.warning("launchd registration failed (non-fatal)", exc_info=True)

            # Onboarding's pywebview left NSApp in Regular activation
            # policy (Dock icon visible). Restore Accessory so the
            # agent runs as a background tray-only app from here on —
            # only pystray's NSStatusItem should be visible. Without
            # this the user sees a Sayzo Dock icon for the agent
            # process AND a second one for the pre-warmed Settings
            # subprocess.
            try:
                from .gui.common.mac_dock import set_dock_visible

                set_dock_visible(False)
            except Exception:
                log.warning("dock-hide after onboarding failed (non-fatal)", exc_info=True)

        # Reload cfg so anything the user changed during onboarding —
        # most importantly the hotkey on the Shortcut screen — flows into
        # the tray seed + ArmController construction below. Without this,
        # the agent registers the stale default hotkey and the tray shows
        # the wrong combo until the user restarts. Settings (which runs
        # out-of-process) sidesteps this by IPC-nudging the live agent;
        # onboarding has no such nudge because the agent doesn't exist yet.
        cfg = load_config()

    from .auth.store import TokenStore
    from .gui.tray import TrayIcon, TrayState, request_full_shutdown

    from .auth.client import make_auth_client
    auth_client = make_auth_client(cfg)
    store = auth_client.store if auth_client is not None else TokenStore(cfg.auth_path)
    upload_client = None
    if auth_client is not None:
        from .upload import AuthenticatedUploadClient

        upload_client = AuthenticatedUploadClient(auth_client, cfg.captures_dir)
        log.warning("uploads enabled → %s", cfg.auth.effective_server_url)

    # Hoist tray construction above the heavy ``.app`` / ``.notify`` import
    # chain so the menubar icon paints in ~1 s instead of 8–10 s on cold
    # boot. ``mark_starting()`` is what gates the arm-toggle menu item
    # against the not-yet-constructed ArmController — without it, a click
    # during boot would queue an arm against ``None``.
    tray_state = TrayState(hotkey_display=humanize_binding(cfg.arm.hotkey))
    tray_state.mark_starting()
    tray = TrayIcon(tray_state, cfg.captures_dir)

    # User-click launch (no primary was running, parent process looks like
    # the OS shell) → surface the Settings window once the agent boots so
    # the user gets visual confirmation. Auto-start paths (Task Scheduler /
    # launchd) and the post-onboarding-finish path (where SetupWindow just
    # closed) stay silent. The flag is on tray_state so _tray_bridge picks
    # it up via its existing settings-event polling — no new wiring.
    from .launch_source import looks_user_launched
    from .update_apply import take_open_settings_after_update

    # Did we just apply a *user-initiated* update (About "Install update" /
    # tray "Install…" / HUD "Install now")? The old agent left this marker
    # before handing off to the installer; consume it every boot so a stale
    # one (e.g. a failed helper spawn) self-clears.
    _user_update = take_open_settings_after_update(cfg.data_dir)
    # ``_pending_upgrade_toast`` (set above) is non-None iff this boot is the
    # first after a version bump — i.e. a staged update was just applied.
    _post_upgrade = _pending_upgrade_toast is not None

    if _user_update and not should_show_gui:
        # ``not should_show_gui`` guard: an update relaunch never passes
        # --force-setup and lands with setup already complete, so this is
        # belt-and-braces against ever popping Settings on top of a setup
        # window (matches the looks_user_launched branch's guard below).
        log.warning(
            "service: user-initiated update applied — re-opening Settings on About"
        )
        tray_state.settings_pane = "About"
        tray_state.settings_event.set()
    elif _post_upgrade:
        # Silent boot-time auto-apply: the downstream "Sayzo updated" toast is
        # the only surface — do NOT pop Settings. This deliberately overrides
        # the Windows ``--open-settings`` flag (the NSIS relaunch passes it for
        # BOTH auto and user applies) and the macOS ``looks_user_launched()``
        # true-positive on the ``open --args service`` relaunch.
        log.warning(
            "service: auto-update relaunch — suppressing Settings auto-open (toast only)"
        )
    elif open_settings:
        log.warning(
            "service: --open-settings flag — auto-opening Settings"
        )
        tray_state.settings_event.set()
    elif not should_show_gui and not from_autostart and looks_user_launched():
        log.warning(
            "service: user-click launch detected — auto-opening Settings"
        )
        tray_state.settings_event.set()

    # Heavy-import bootstrap. Defers ``from .app import Agent``, ``from
    # .notify import …``, and the Agent constructor — all together 2–5 s on
    # the first cold path due to numpy / scipy / silero / av / pywebview
    # loading lazily — until AFTER the tray icon has painted.
    #
    # Dispatcher routes this:
    #   - macOS:   spawned on a worker thread BEFORE ``tray.run_main()``
    #              so the main thread is free to host ``NSApp.run()``,
    #              which is what actually paints the menubar item.
    #   - Windows: called synchronously AFTER ``tray.start()`` has
    #              handed icon paint to a daemon thread.
    #
    # ``agent`` is assigned via ``nonlocal`` so ``_main`` (defined
    # below) resolves it at call time. Python's lexical scoping
    # tolerates the forward reference: ``agent`` doesn't need to
    # exist when ``_main`` is parsed, only when it executes — and
    # by then ``_build_pipeline_state()`` has run.
    #
    # ``mark_ready()`` flips the tray's ``_starting`` flag once Agent
    # is wired up, so the tooltip + arm-toggle stop saying "Starting…"
    # and start dispatching real arm/disarm events.
    agent = None

    # HudLauncher manages the HUD subprocess lifetime. Constructed inside
    # _build_pipeline_state (sync) so it's available before _main runs, but
    # .start() is called inside _main where the asyncio loop is live.
    hud_launcher: "HudLauncher | None" = None

    # The Notifier bound to the HUD launcher. nonlocal so _main can fire the
    # post-upgrade toast AFTER the HUD subprocess is ready (see _main).
    notifier = None

    def _build_pipeline_state() -> None:
        nonlocal agent, hud_launcher, notifier
        from .app import Agent
        from .gui.hud.launcher import HudLauncher
        from .notify import HudNotifier, NoopNotifier

        hud_launcher = (
            HudLauncher(heartbeat_secs=cfg.hud.heartbeat_secs)
            if cfg.notifications_enabled else None
        )
        notifier = (
            HudNotifier(hud_launcher)
            if hud_launcher is not None else NoopNotifier()
        )
        # Surface a "Notifications unavailable" tray line when the HUD
        # respawn ladder gives up, and clear it when a later arm recovers it
        # (see HudLauncher.reset_given_up, called from the arm path).
        if hud_launcher is not None:
            def _on_hud_health(ok: bool) -> None:
                try:
                    if tray_state.set_hud_degraded(not ok):
                        tray.update()
                except Exception:
                    log.debug("[hud] health->tray update raised", exc_info=True)
            hud_launcher.set_health_callback(_on_hud_health)

        # Share the notifier + pre-quit hook with the tray so both the
        # IPC QUIT_AGENT path and the tray Quit menu can surface a
        # "Sayzo is updating…" toast when this quit will trigger a staged
        # auto-update apply (Phase B, v2.8.2). Without this, the dead
        # zone between Settings closing and the new agent reappearing is
        # silent — users can reasonably think Sayzo crashed and try to
        # reopen it mid File /r.
        tray_state.notifier = notifier

        def _fire_pre_apply_toast() -> None:
            # Only fires when the user explicitly chose "install now" —
            # plain tray Quit leaves the flag absent, so we don't promise
            # a reappearance that won't happen.
            try:
                from .update_stage import read_staged
                from .update import is_newer
                from .update_apply import has_quit_apply_intent

                if not has_quit_apply_intent(cfg.data_dir):
                    return
                staged = read_staged(cfg.data_dir)
                if (staged is not None
                        and is_newer(__version__, staged.version)
                        and cfg.notifications_enabled):
                    notifier.notify(
                        "Sayzo is updating",
                        f"Installing v{staged.version}. "
                        "Sayzo will reappear shortly.",
                    )
            except Exception:
                log.debug("[update] pre-apply toast failed", exc_info=True)

        tray_state.pre_quit_hook = _fire_pre_apply_toast

        # Shared by the HUD "Install now" toast (see _update_check) and the
        # tray "Install Sayzo vX.Y.Z" menu (see gui/tray.py::on_open_update).
        # Settings → Install update writes the same flag from its own
        # subprocess via IPC QUIT_AGENT — three surfaces, one mechanism.
        def _on_install_update_clicked() -> None:
            try:
                from .update_apply import set_quit_apply_intent
                set_quit_apply_intent(cfg.data_dir)
                request_full_shutdown(tray_state)
            except Exception:
                log.warning(
                    "[update] install-update click handler raised",
                    exc_info=True,
                )

        tray_state.on_install_update_clicked = _on_install_update_clicked

        # NOTE: the post-upgrade "Sayzo updated" toast is fired in _main AFTER
        # the HUD subprocess handshakes hud_ready — NOT here. Firing it during
        # this sync setup (before hud_launcher.start()) meant _send hit
        # `_proc is None` and dropped the toast on every auto-update (observed
        # 3x in one production log). _pending_upgrade_toast stays set so _main
        # picks it up; see _fire_post_upgrade_toast below.

        # NOTE: the apply-failed toast is also fired in _main after hud_ready
        # (same pre-spawn drop race as the post-upgrade toast above).
        # _pending_apply_failed_toast stays set for _fire_post_upgrade_toast.

        agent = Agent(
            cfg,
            upload_client=upload_client,
            notifier=notifier,
            auth_client=auth_client,
        )

        from .account import decide_arm_gate, read_cache as _read_account_cache

        def _account_gate_fn():
            try:
                cached = _read_account_cache(cfg)
            except Exception:
                log.warning("[arm] account cache read raised; allowing", exc_info=True)
                cached = None
            return decide_arm_gate(
                cached, enabled=cfg.auth.account_check_enabled
            )

        agent.arm.account_gate_fn = _account_gate_fn

        # The auth-required upload toast's "Sign in" button routes here (fired
        # from the HUD reader thread). Re-open Settings on the Account pane so
        # the user can run the desktop sign-in that actually clears the auth
        # block — a web login wouldn't refresh the agent's OAuth token. Setting
        # the pane field + event is the same thread-safe handoff the tray uses.
        def _on_sign_in_requested() -> None:
            tray_state.settings_pane = "Account"
            tray_state.settings_event.set()

        agent.retry_mgr.set_sign_in_callback(_on_sign_in_requested)

        # Tray is now backed by a real ArmController. Stop showing the
        # bootstrap "Starting…" copy and let menu clicks dispatch.
        tray_state.mark_ready()
        tray.update()

    def _fire_post_upgrade_toast() -> None:
        """Fire the queued post-upgrade / apply-failed toast (called from
        _main AFTER the HUD is ready — see the call site for why). Best-effort:
        a notifier failure logs but never blocks startup. The two markers are
        mutually exclusive in practice (update worked vs. update failed)."""
        if notifier is None:
            return
        try:
            if _pending_upgrade_toast is not None:
                _prior_v, _now_v = _pending_upgrade_toast
                if _diagnostics_disclosure_due and cfg.share_diagnostics:
                    # Proactive opt-out disclosure for upgraders (advisor
                    # catch): they never re-run onboarding, so this is their
                    # notice that diagnostics turned on. No em-dash in copy.
                    notifier.notify(
                        "Sayzo updated",
                        f"Now on v{_now_v}. To help us fix problems, Sayzo "
                        "shares anonymous diagnostics (your OS, app version, "
                        "and error logs, never meeting audio or transcripts). "
                        "Manage this in Settings under About.",
                    )
                else:
                    notifier.notify("Sayzo updated", f"Now running v{_now_v}.")
                log.info(
                    "[update] post-upgrade toast fired (v%s -> v%s, "
                    "disclosure=%s)",
                    _prior_v, _now_v, _diagnostics_disclosure_due,
                )
            if _pending_apply_failed_toast is not None:
                notifier.notify(
                    "Sayzo update failed",
                    f"Couldn't install v{_pending_apply_failed_toast}. "
                    "Download the latest from sayzo.app to update manually.",
                )
                log.info(
                    "[update] apply-failed toast fired for v%s",
                    _pending_apply_failed_toast,
                )
        except Exception:
            log.warning("[update] post-upgrade toast failed", exc_info=True)

    async def _main() -> None:
        loop = asyncio.get_running_loop()

        def _handle_stop() -> None:
            log.warning("shutdown requested")
            tray.stop()
            agent.stop()

        try:
            loop.add_signal_handler(signal.SIGINT, _handle_stop)
            loop.add_signal_handler(signal.SIGTERM, _handle_stop)
        except (NotImplementedError, RuntimeError):
            pass

        # Windows: Task Scheduler sends SIGBREAK (Ctrl+Break) on stop.
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, lambda *_: _handle_stop())

        # IPC server for the Settings subprocess. Registers just the methods
        # whose effect requires the live in-process agent (token-store cache
        # invalidation, hotkey rebinding); pure-data reads continue to go
        # through the file-based settings_store directly. Phase 4 extends
        # this with mic-holder + detector mutation methods. The server is
        # started before the agent's main loop so a Settings window opened
        # immediately after `service` boots can connect.
        from .gui.settings.ipc import IPCServer, Methods

        ipc_server = IPCServer(cfg.data_dir)
        ipc_server.register(Methods.PING, lambda: "pong")

        def _ipc_invalidate_token_cache() -> None:
            try:
                store.invalidate_cache()
            except Exception:
                log.debug("[ipc] invalidate_token_cache failed", exc_info=True)

        def _ipc_rebind_hotkey(binding: str) -> dict:
            err = agent.arm.rebind_hotkey(binding)
            if err is None:
                # Push the new binding into the tray immediately rather
                # than waiting up to 500 ms for the next _tray_bridge tick.
                # Without this, users who change their hotkey in Settings
                # see the old binding lingering in the tray menu / tooltip
                # until the polling loop catches up — long enough that
                # they assume the change didn't apply.
                tray_state.set_hotkey_display(
                    humanize_binding(agent.arm.current_hotkey)
                )
                tray.update()
            return {"error": err}

        def _ipc_snapshot_mic_state() -> dict:
            # Shaped to match React's MicStateSnapshot type. ``holders`` is
            # the only field the Add-app dialog actually reads on Windows;
            # macOS leans on ``active`` + foreground bundle id (separate
            # method) since CoreAudio has no per-process attribution.
            #
            # ``is_browser`` is computed here so the polling React side
            # doesn't need an extra IPC roundtrip per row to filter
            # browsers out of the desktop-app picker.
            from .arm.detectors import BROWSER_PROCESS_NAMES
            state = agent.arm.snapshot_mic_state()
            return {
                "holders": [
                    {
                        "process_name": h.process_name,
                        "pid": h.pid,
                        "is_browser": h.process_name.lower() in BROWSER_PROCESS_NAMES,
                    }
                    for h in state.holders
                ],
                "active": state.active,
                "running_processes": sorted(state.running_processes),
            }

        def _ipc_snapshot_foreground() -> dict:
            fg = agent.arm.snapshot_foreground()
            return {
                "process_name": fg.process_name,
                "bundle_id": fg.bundle_id,
                "window_title": fg.window_title,
                "browser_tab_url": fg.browser_tab_url,
                "browser_tab_title": fg.browser_tab_title,
                "is_browser": fg.is_browser,
                "browser_window_titles": list(fg.browser_window_titles),
                "browser_window_urls": list(fg.browser_window_urls),
            }

        def _ipc_reload_detectors() -> dict:
            ok = agent.arm.reload_detectors()
            return {"reloaded": ok}

        def _ipc_snapshot_processing_captures() -> dict:
            # Shallow copy — the dict is mutated only on the agent's asyncio
            # loop (same loop the IPC handler runs on), so a snapshot here
            # is consistent without locking.
            return {k: dict(v) for k, v in agent._processing_state.items()}

        def _ipc_nudge_upload_retry() -> dict:
            if agent._sweep_in_progress:
                return {"started": False, "reason": "already_running"}
            agent._sweep_in_progress = True
            agent._upload_sweep_last = time.monotonic()
            # User-triggered: clear any active credit pause before sweeping
            # so a stale 24h lockout doesn't block a top-up that already
            # happened server-side.
            task = asyncio.create_task(agent._run_user_triggered_sweep())
            agent._background_tasks.add(task)
            task.add_done_callback(agent._background_tasks.discard)
            return {"started": True}

        def _ipc_open_settings() -> dict:
            # Surface the Settings window. Same trigger the tray menu uses,
            # so the show path (pre-warmed --idle subprocess, hide-on-close
            # contract, dock-icon toggle on macOS) is identical. _tray_bridge
            # picks the event up on its next 0.5 s tick.
            tray_state.settings_event.set()
            return {"ok": True}

        def _ipc_quit_agent() -> dict:
            # Same shape as the tray Quit menu — see tray.request_full_shutdown
            # for why macOS must unload launchd before quit_event fires.
            log.info("[ipc] quit_agent received from Settings")
            request_full_shutdown(tray_state)
            return {"ok": True}

        def _ipc_reload_notification_config() -> dict:
            try:
                fresh = load_config()
            except Exception:
                log.warning(
                    "[ipc] reload_notification_config: load_config failed",
                    exc_info=True,
                )
                return {"reloaded": False}
            # Copy the master + post-capture-feedback flags onto the live
            # agent cfg so the CapturePoller (which reads them at fire time,
            # minutes after upload) sees a Settings → Notifications toggle
            # without a restart. Re-reading from disk preserves env-var
            # precedence (SAYZO_NOTIFY_CAPTURE_FEEDBACK / SAYZO_NOTIFICATIONS_ENABLED
            # still win).
            try:
                agent.cfg.notify_capture_feedback = fresh.notify_capture_feedback
                agent.cfg.notifications_enabled = fresh.notifications_enabled
                # share_diagnostics rides the same reload — the diagnostics
                # headers / crash sweep / on-demand pull all read it live off
                # agent.cfg, so a Settings toggle takes effect without a
                # restart.
                agent.cfg.share_diagnostics = fresh.share_diagnostics
            except Exception:
                log.warning(
                    "[ipc] reload_notification_config: cfg apply raised",
                    exc_info=True,
                )
            return {"reloaded": True}

        def _ipc_reload_hud_config() -> dict:
            # Settings → Recording "Show recording indicator" toggle (and the
            # first-run onboarding picker) write the new value to
            # user_settings.json from their own subprocess, then nudge here.
            # Re-read the full config from disk and copy just the HUD field
            # onto the live agent's cfg so the next _arm_internal sees it.
            # Re-reading (vs. taking the value as a param) preserves env-var
            # precedence: SAYZO_HUD__SHOW_RECORDING_INDICATOR still wins.
            try:
                fresh = load_config()
            except Exception:
                log.warning(
                    "[ipc] reload_hud_config: load_config failed", exc_info=True
                )
                return {"reloaded": False}
            try:
                agent.cfg.hud.show_recording_indicator = (
                    fresh.hud.show_recording_indicator
                )
            except Exception:
                log.warning(
                    "[ipc] reload_hud_config: apply raised", exc_info=True
                )
                return {"reloaded": False}
            log.info(
                "[ipc] reload_hud_config: show_recording_indicator=%s",
                agent.cfg.hud.show_recording_indicator,
            )
            return {"reloaded": True}

        ipc_server.register(Methods.INVALIDATE_TOKEN_CACHE, _ipc_invalidate_token_cache)
        ipc_server.register(Methods.REBIND_HOTKEY, _ipc_rebind_hotkey)
        ipc_server.register(Methods.SNAPSHOT_MIC_STATE, _ipc_snapshot_mic_state)
        ipc_server.register(Methods.SNAPSHOT_FOREGROUND, _ipc_snapshot_foreground)
        ipc_server.register(Methods.RELOAD_DETECTORS, _ipc_reload_detectors)
        ipc_server.register(
            Methods.SNAPSHOT_PROCESSING_CAPTURES, _ipc_snapshot_processing_captures
        )
        ipc_server.register(Methods.NUDGE_UPLOAD_RETRY, _ipc_nudge_upload_retry)
        ipc_server.register(Methods.OPEN_SETTINGS, _ipc_open_settings)
        ipc_server.register(Methods.QUIT_AGENT, _ipc_quit_agent)
        ipc_server.register(
            Methods.RELOAD_NOTIFICATION_CONFIG, _ipc_reload_notification_config
        )
        ipc_server.register(Methods.RELOAD_HUD_CONFIG, _ipc_reload_hud_config)

        try:
            await ipc_server.start()
        except Exception:
            log.warning("[ipc] server failed to start — Settings will fall back to file-only paths", exc_info=True)

        # Bridge the tray thread with the asyncio loop: poll for user clicks
        # on the Arm/Stop / Settings... / Quit menu items, and push the
        # ArmController's state back to the tray so labels and tooltip stay
        # in sync across armings.
        from .arm import ArmState
        from .gui.tray import Status as TrayStatus

        def _settings_subprocess_argv() -> list[str]:
            """Build ``argv`` for ``sayzo-agent settings``.

            Frozen builds ship a single binary that already routes ``settings``
            via Click. Dev runs use ``python -m sayzo_agent settings`` so the
            entry point is found inside the venv without relying on the
            console-script shim being on PATH.
            """
            if getattr(sys, "frozen", False):
                return [sys.executable, "settings"]
            return [sys.executable, "-m", "sayzo_agent", "settings"]

        class _SettingsLauncher:
            """Manage a single pre-warmed ``sayzo-agent settings --idle``
            subprocess across the agent's lifetime.

            Spawned at agent boot, the subprocess holds a hidden pywebview
            window — paying the Python startup + WebView2 init cost up front
            so the tray's Settings... click drops to an instant ``show``.
            On the user's window-close (X button) the subprocess hides
            instead of destroying, so the next click is also instant.

            The agent owns the lifecycle: ``quit()`` is called in shutdown
            to guarantee no orphan Settings process survives the agent.
            EOF on the subprocess's stdin pipe (i.e. our process dies
            without sending quit) triggers the same teardown on the child.
            """

            def __init__(self) -> None:
                self._proc: asyncio.subprocess.Process | None = None
                self._lock = asyncio.Lock()

            async def start(self) -> None:
                """Spawn the idle subprocess. No-op if already running."""
                if self._proc is not None and self._proc.returncode is None:
                    return
                argv = _settings_subprocess_argv() + ["--idle"]
                log.info("[settings] pre-spawning idle subprocess: %s", argv)
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *argv, stdin=asyncio.subprocess.PIPE,
                    )
                except Exception:
                    log.warning(
                        "[settings] failed to pre-spawn idle subprocess",
                        exc_info=True,
                    )
                    self._proc = None

            async def show(self, pane: str | None = None) -> None:
                """Make the Settings window visible, optionally on ``pane``.

                Respawns the subprocess first if it died (manual kill, OOM,
                crash) so a one-time failure doesn't permanently break the
                tray menu's Settings click. ``pane`` (e.g. ``"Account"`` /
                ``"About"``) routes through the ``show:<pane>`` stdin command
                so the already-mounted React app navigates at runtime — see
                ``gui/settings/window.py::_stdin_command_loop``.
                """
                async with self._lock:
                    if pane:
                        await self._send(f"show:{pane}\n".encode())
                    else:
                        await self._send(b"show\n")

            async def _send(self, payload: bytes) -> None:
                if self._proc is None or self._proc.returncode is not None:
                    await self.start()
                if self._proc is None or self._proc.stdin is None:
                    return
                try:
                    self._proc.stdin.write(payload)
                    await self._proc.stdin.drain()
                    return
                except (BrokenPipeError, ConnectionResetError):
                    log.info("[settings] pipe broken — respawning")
                self._proc = None
                await self.start()
                if self._proc is None or self._proc.stdin is None:
                    return
                try:
                    self._proc.stdin.write(payload)
                    await self._proc.stdin.drain()
                except Exception:
                    log.warning("[settings] resend failed", exc_info=True)

            async def quit(self) -> None:
                """Tell the subprocess to destroy its window and exit.

                Bounded total: 3 s graceful + 2 s after terminate before
                kill. Called from shutdown paths so the agent never leaves
                an orphan Settings process behind.
                """
                async with self._lock:
                    proc = self._proc
                    self._proc = None
                    if proc is None or proc.returncode is not None:
                        return
                    log.info("[settings] sending quit to idle subprocess")
                    try:
                        if proc.stdin is not None:
                            proc.stdin.write(b"quit\n")
                            await proc.stdin.drain()
                            proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
                        return
                    except asyncio.TimeoutError:
                        log.warning(
                            "[settings] subprocess didn't quit in 3 s — terminating",
                        )
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        return
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        log.warning("[settings] terminate didn't take — killing")
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            return
                        # Confirm the kill is reaped before returning — a
                        # killed-but-not-yet-reaped Settings process still
                        # holds the exe/DLL image handles, which the silent
                        # update installer's File /r would then race.
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            log.warning("[settings] kill not reaped in 2 s — proceeding")

        settings_launcher = _SettingsLauncher()
        await settings_launcher.start()

        # HUD subprocess: spawn now so it's warm by the time the user
        # first arms (whitelist or hotkey). The launcher itself was
        # constructed in _build_pipeline_state so the notifier could
        # bind to it; .start() spawns the actual subprocess and brings
        # up the stdout reader.
        if hud_launcher is not None:
            await hud_launcher.start()
            _install_agent_side_hud_shutdown_propagation(hud_launcher)
            # Fire the post-upgrade / apply-failed toast now that the HUD
            # subprocess exists — but only after it handshakes hud_ready, so
            # the toast isn't dropped against `_proc is None`. Bounded wait so
            # a sick HUD can't stall boot. Best-effort: a drop here is no worse
            # than the old always-dropped behavior, and the update still
            # applied either way.
            if (
                cfg.notifications_enabled
                and (_pending_upgrade_toast is not None
                     or _pending_apply_failed_toast is not None)
            ):
                try:
                    await hud_launcher.wait_for_ready(timeout_secs=15.0)
                except Exception:
                    log.debug("[update] wait_for_ready before toast raised",
                              exc_info=True)
                _fire_post_upgrade_toast()

        def _sync_arm_state_to_tray() -> None:
            """Push ArmController.state → TrayState immediately.

            Called both from the bridge's 0.5 s poll and (synchronously, on
            the asyncio loop) from ArmController's state-change callback so
            menu labels reflect arm/disarm the moment the transition
            finishes — not up to 0.5 s later. Without this the user saw
            "right-click menu still says Start recording" right after
            arming.

            Also re-seeds the hotkey display from ``agent.arm.current_hotkey``
            so a user with a custom binding (loaded from
            ``user_settings.json`` at boot) doesn't see the dataclass
            default ``"Ctrl+Alt+S"`` flash in the menu before
            ``_tray_bridge``'s first poll catches up.
            """
            cur = agent.arm.state
            tray_state.set_status(
                TrayStatus.ARMED if cur == ArmState.ARMED else TrayStatus.DISARMED
            )
            tray_state.set_hotkey_display(
                humanize_binding(agent.arm.current_hotkey)
            )
            tray.update()

        agent.arm.set_state_change_callback(_sync_arm_state_to_tray)

        async def _tray_bridge() -> None:
            last_hotkey: str | None = None
            # Seed the tray with the current state once; subsequent pushes
            # come from ArmController's state-change callback.
            _sync_arm_state_to_tray()
            while not agent._stop.is_set():
                await asyncio.sleep(0.5)
                if tray_state.quit_event.is_set():
                    _handle_stop()
                    return
                # User clicked the Arm/Stop menu item. ArmController handles
                # the transition atomically (no confirmation toast) and
                # drops rapid double-clicks via its own in-flight flag.
                if tray_state.arm_toggle_event.is_set():
                    tray_state.arm_toggle_event.clear()
                    asyncio.create_task(agent.arm.arm_from_tray())
                # Settings... — show the pre-warmed Settings subprocess.
                # The subprocess was spawned at agent boot and holds a
                # hidden pywebview window; ``show`` makes it visible
                # without paying the Python+WebView2 startup cost again.
                # The subprocess owns its own main thread, which is what
                # Cocoa needs on macOS (pystray holds the agent's main
                # thread, so an in-process pywebview can't run there) and
                # avoids Tcl thread-affinity surprises on Windows.
                if tray_state.settings_event.is_set():
                    tray_state.settings_event.clear()
                    # Read + clear the optional target pane published right
                    # before the event was set (the Event's set/is_set pair
                    # provides the memory barrier). ``None`` → last-viewed pane.
                    _pane = tray_state.settings_pane
                    tray_state.settings_pane = None
                    asyncio.create_task(settings_launcher.show(pane=_pane))
                if tray_state.finish_setup_event.is_set():
                    tray_state.finish_setup_event.clear()
                    cached = tray_state.get_cached_account()
                    url = (cached.onboarding_url if cached else None) or (
                        cfg.auth.effective_server_url.rstrip("/") + "/onboarding"
                        if cfg.auth.effective_server_url else None
                    )
                    if url:
                        try:
                            import webbrowser as _wb
                            _wb.open(url, new=2)
                        except Exception:
                            log.warning(
                                "[tray] webbrowser.open failed for %s", url,
                                exc_info=True,
                            )
                # Belt-and-braces poll sync in case the callback ever fails
                # to fire (e.g. state changed before the callback was wired).
                _sync_arm_state_to_tray()
                cur_hotkey = agent.arm.current_hotkey
                if cur_hotkey != last_hotkey:
                    tray_state.set_hotkey_display(humanize_binding(cur_hotkey))
                    # On Linux/GTK pystray's menu is long-lived; without an
                    # explicit update_menu() the new label sits in
                    # TrayState but doesn't surface until the next icon
                    # repaint. Windows / macOS rebuild on each menu open
                    # via the callable text, so this is a no-op there.
                    tray.update()
                    last_hotkey = cur_hotkey

        # Phase B auto-update: poll the manifest, download + verify SHA256.
        # The swap is NOT applied here — see update_apply.py for the apply
        # half. Fires a one-shot "New version available" HUD toast when a
        # fresh stage lands; the per-version early-return below ensures the
        # toast only fires once per release. Failures swallowed so a flaky
        # manifest fetch never breaks capture. Env overrides
        # SAYZO_UPDATE_CHECK_INITIAL_DELAY_SECS + ..._INTERVAL_SECS exist
        # for E2E tests.
        async def _update_check() -> None:
            from . import __version__
            from .update import check
            from .update_stage import (
                clear_staged,
                download_and_stage,
                read_staged,
            )
            from .gui.tray import UpdateOffer

            initial_delay = float(
                os.environ.get("SAYZO_UPDATE_CHECK_INITIAL_DELAY_SECS", 60)
            )
            interval = float(
                os.environ.get("SAYZO_UPDATE_CHECK_INTERVAL_SECS", 6 * 60 * 60)
            )
            try:
                await asyncio.sleep(initial_delay)
            except asyncio.CancelledError:
                return

            while not agent._stop.is_set():
                try:
                    info = await check(__version__)
                except Exception:
                    log.warning("[update] check failed", exc_info=True)
                    info = None

                if info is None:
                    tray_state.set_update_offer(None)
                else:
                    # Avoid re-downloading the same release on every poll.
                    # If the stage on disk already matches what the manifest
                    # advertises, we're done until a newer version ships.
                    current_stage = read_staged(cfg.data_dir)
                    if current_stage is not None and current_stage.version == info.version:
                        log.info(
                            "[update] v%s already staged at %s",
                            info.version, current_stage.payload_path,
                        )
                        tray_state.set_update_offer(
                            UpdateOffer(version=info.version, url=info.url)
                        )
                    else:
                        if current_stage is not None:
                            # Older stage on disk, newer one in manifest.
                            # Wipe the old before we re-stage so two
                            # payloads don't coexist mid-download.
                            log.info(
                                "[update] replacing stale stage v%s with v%s",
                                current_stage.version, info.version,
                            )
                            clear_staged(cfg.data_dir)
                        try:
                            staged = await download_and_stage(info, cfg.data_dir)
                        except Exception:
                            log.warning(
                                "[update] download_and_stage raised", exc_info=True
                            )
                            staged = None
                        if staged is not None:
                            tray_state.set_update_offer(
                                UpdateOffer(
                                    version=staged.version, url=info.url
                                )
                            )
                            if cfg.notifications_enabled:
                                try:
                                    tray_state.notifier.notify_actionable(
                                        title="New version available",
                                        body=(
                                            f"Sayzo v{staged.version} "
                                            "is ready to install."
                                        ),
                                        button_label="Install now",
                                        on_pressed=tray_state.on_install_update_clicked,
                                        expire_after_secs=60.0,
                                    )
                                except Exception:
                                    log.warning(
                                        "[update] install-ready toast raised",
                                        exc_info=True,
                                    )

                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

        # --- Remote diagnostics (v3.16+) -------------------------------------
        # Shared in-flight guard so the on-demand pull and the boot crash
        # sweep never run two concurrent uploads. All reads of share_diagnostics
        # / auth_client are live off the enclosing scope, so a Settings toggle
        # (applied via RELOAD_NOTIFICATION_CONFIG) takes effect immediately.
        _diag_in_flight = {"v": False}

        async def _run_diagnostics_upload(reason: str) -> bool:
            if _diag_in_flight["v"]:
                return False
            # Read share_diagnostics off agent.cfg — that's the object the
            # RELOAD_NOTIFICATION_CONFIG IPC handler mutates, so a Settings
            # toggle takes effect here without a restart.
            if auth_client is None or not agent.cfg.share_diagnostics:
                return False
            _diag_in_flight["v"] = True
            try:
                from .diagnostics import DiagnosticsUploader
                return await DiagnosticsUploader(auth_client, cfg).try_upload(reason)
            finally:
                _diag_in_flight["v"] = False

        def _spawn_diagnostics(reason: str) -> None:
            task = asyncio.create_task(_run_diagnostics_upload(reason))
            agent._background_tasks.add(task)
            task.add_done_callback(agent._background_tasks.discard)

        async def _crash_report_sweep() -> None:
            """One-shot at boot: if the previous run left a crash sentinel and
            the user shares diagnostics, upload agent.log once and clear the
            sentinel. On opt-out, discard the sentinel without uploading; when
            signed out, keep it for a later boot."""
            from .diagnostics import crash_sentinel_path
            sentinel = crash_sentinel_path(cfg.data_dir)
            if not sentinel.exists():
                return
            if not agent.cfg.share_diagnostics:
                try:
                    sentinel.unlink()
                except OSError:
                    pass
                return
            if auth_client is None:
                return
            if await _run_diagnostics_upload("crash"):
                try:
                    sentinel.unlink()
                except OSError:
                    pass

        async def _account_refresh() -> None:
            """Background /api/me refresh. The arm-time gate reads from the
            on-disk cache that this task populates — so a slow first fetch
            never blocks arming, and a flaky network never locks the user
            out (the gate falls through to "allow" on missing cache)."""
            if not cfg.auth.account_check_enabled:
                log.info(
                    "[account] check disabled (SAYZO_AUTH__ACCOUNT_CHECK_ENABLED=0)"
                )
                return
            if auth_client is None:
                log.info(
                    "[account] no auth client (not signed in) — skipping refresh"
                )
                return

            from .account import read_cache as _read_cache, refresh_and_cache

            try:
                seed = _read_cache(cfg)
                if seed is not None:
                    tray_state.set_cached_account(seed)
                    tray.update()
            except Exception:
                log.debug("[account] tray seed from cache raised", exc_info=True)

            interval = max(60.0, float(cfg.auth.account_refresh_interval_secs))
            while not agent._stop.is_set():
                try:
                    response = await refresh_and_cache(auth_client, cfg)
                    log.info(
                        "[account] refresh: status=%s onboarding_complete=%s",
                        response.status, response.onboarding_complete,
                    )
                    if response.is_persistable:
                        # Re-read the cache so the tray reflects exactly
                        # what the gate will read; also lets us skip the
                        # tray rebuild when the persisted state hasn't
                        # actually changed.
                        fresh = _read_cache(cfg)
                        if fresh is not None and tray_state.set_cached_account(fresh):
                            tray.update()
                    # On-demand diagnostics pull: the server flags a specific
                    # user from the admin dashboard; we ship the log on the
                    # next poll. Gated on the opt-out toggle.
                    if response.collect_logs and agent.cfg.share_diagnostics:
                        log.info(
                            "[account] server requested diagnostics — "
                            "firing one-shot log upload"
                        )
                        _spawn_diagnostics("on_demand")
                except Exception:
                    log.warning("[account] refresh raised", exc_info=True)
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

        asyncio.create_task(_tray_bridge())
        asyncio.create_task(_update_check())
        asyncio.create_task(_account_refresh())
        asyncio.create_task(_crash_report_sweep())
        try:
            await agent.run()
        finally:
            # Shut the Settings subprocess down before the IPC server so
            # its in-flight bridge calls can resolve cleanly (and so we
            # don't leave an orphan pywebview process if SIGKILL fires
            # later in the macOS force-exit path).
            await settings_launcher.quit()
            if hud_launcher is not None:
                await hud_launcher.quit()
            await ipc_server.stop()

    try:
        if sys.platform == "darwin":
            # macOS dispatch: heavy bootstrap on a worker thread, NSApp on
            # the main thread.
            #
            # 1. Spawn worker that runs ``_build_pipeline_state()`` then
            #    ``asyncio.run(_main())``. Heavy imports + Agent +
            #    notify backend init all happen here — off the main
            #    thread so the tray icon paint at step 3 isn't blocked
            #    by 4–10 s of cold imports.
            # 2. Install the NSApplicationDelegate hook for app-reopen
            #    events (Dock click / Spotlight launch / ``open -a Sayzo``
            #    while running). LSUIElement=True bundles don't spawn a
            #    second process for those — LaunchServices delivers a
            #    ``kAEReopenApplication`` Apple Event to the existing
            #    process, which the delegate picks up. (Windows handles
            #    the equivalent via the IPC ``OPEN_SETTINGS`` path; the
            #    fresh process there hits the kernel mutex and nudges
            #    the primary.)
            # 3. ``tray.run_main()`` — calls ``NSApp.run()`` which does
            #    ``[NSApp finishLaunching]`` (where AppKit registers its
            #    own kAEReopenApplication handler — the
            #    NSApplicationDelegate path survives that), then drives
            #    the runloop until tray quit.
            asyncio_exc: list[BaseException] = []

            def _asyncio_runner() -> None:
                try:
                    _build_pipeline_state()
                    asyncio.run(_main())
                except BaseException as e:
                    asyncio_exc.append(e)
                finally:
                    tray.stop()

            worker = threading.Thread(target=_asyncio_runner, name="asyncio", daemon=False)
            worker.start()

            try:
                from .gui.common.mac_reopen import install_reopen_handler

                install_reopen_handler(tray_state.settings_event.set)
            except Exception:
                log.warning(
                    "[mac_reopen] install failed — Dock-click won't open Settings",
                    exc_info=True,
                )

            try:
                tray.run_main()
            finally:
                # After the tray closes on macOS, force-exit unconditionally.
                # Several things can keep the Python process alive past the
                # main thread even when the asyncio worker exits cleanly:
                #
                #   - pyobjc / pywebview leave non-daemon threads bound to
                #     AppKit internals after the setup webview is destroyed.
                #   - pythonnet / clr_loader initialize the CoreCLR runtime
                #     which runs a background GC thread.
                #   - launchd-spawned XPC helpers (WebKit Networking, GPU)
                #     can land outside our process group.
                #
                # The user clicked Quit and expects "process gone". The
                # launchd plist is already unloaded via the tray's quit
                # handler, so nothing will revive us. SIGKILL the process
                # group first to take down direct children (audio-tap, etc.),
                # then os._exit as a safety net in case killpg raised.
                # ``agent`` is None if the worker crashed before Agent
                # construction finished (heavy imports failing land in
                # ``asyncio_exc``).
                if agent is not None:
                    agent.stop()
                worker.join(timeout=5)
                log.warning("tray quit — killing process group and exiting")

                from .update_apply import apply_staged_at_quit_if_flagged
                apply_staged_at_quit_if_flagged(cfg.data_dir, __version__)

                remove_pid(cfg.pid_path)
                # Best-effort: pkill any remaining direct children that may
                # have escaped the process group (WebKit XPC helpers).
                try:
                    subprocess.run(
                        ["pkill", "-P", str(os.getpid())],
                        timeout=2,
                        capture_output=True,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                except OSError:
                    pass
                os._exit(0)
            if asyncio_exc and not isinstance(asyncio_exc[0], KeyboardInterrupt):
                raise asyncio_exc[0]
        else:
            # Windows / Linux dispatch:
            # 1. ``tray.start()`` paints the system-tray icon on a daemon
            #    thread immediately — that's the user's "Sayzo is alive"
            #    signal, must happen before the heavy import chain.
            # 2. ``_build_pipeline_state()`` runs the heavy imports +
            #    Agent construction synchronously on the main thread.
            #    By the time it returns, the tray icon is already up
            #    and the user perceives launch as ~instant even though
            #    asyncio hasn't started yet.
            # 3. ``asyncio.run(_main())`` blocks the main thread until
            #    shutdown (Ctrl+C / tray Quit / SIGTERM).
            tray.start()
            _build_pipeline_state()
            asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
        from .update_apply import apply_staged_at_quit_if_flagged
        apply_staged_at_quit_if_flagged(cfg.data_dir, __version__)
        remove_pid(cfg.pid_path)
        log.warning("sayzo-agent service stopped")


@cli.command()
@click.option(
    "--pane",
    default=None,
    help="Open the Settings window with this pane selected (e.g. Account, About).",
)
@click.option(
    "--idle",
    is_flag=True,
    default=False,
    help=(
        "Pre-warm mode: open the window hidden, hide on user close, accept "
        "show/hide/quit commands on stdin. Spawned by the agent at startup so "
        "the tray's Settings... click feels instant."
    ),
)
def settings(pane: str | None, idle: bool) -> None:
    """Open the Settings window in a dedicated pywebview process.

    Spawned by the tray menu's "Settings…" click. Exits 0 when the window
    closes; exits 0 immediately (with a log line) if another Settings
    window already holds the cross-process lock.
    """
    cfg = load_config()
    _setup_logging("INFO", debug=cfg.debug)
    log = logging.getLogger("settings")

    from .gui.settings.lockfile import SettingsLock
    from .gui.settings.window import SettingsWindow

    with SettingsLock(cfg.data_dir) as lock:
        if not lock.acquired:
            log.warning("another Settings window is already open — exiting")
            return
        try:
            SettingsWindow(cfg, pane=pane, idle=idle).run_blocking()
        except Exception:
            log.exception("settings window crashed")


@cli.command()
@click.option(
    "--idle",
    is_flag=True,
    default=False,
    help=(
        "Subprocess mode (default for agent-spawned HUDs). The window "
        "opens immediately but its React app stays in the `hidden` "
        "state until the agent writes a show_pill / show_toast / etc. "
        "command over stdin."
    ),
)
@click.option(
    "--demo",
    is_flag=True,
    default=False,
    help=(
        "Render the in-HUD demo control strip so a developer can click "
        "through each event type. Used by scripts/preview_hud.py."
    ),
)
def hud(idle: bool, demo: bool) -> None:
    """Open the HUD overlay window in a dedicated pywebview process.

    Spawned by the agent at boot via :class:`HudLauncher`. Exits 0 when
    the window closes or the parent's stdin EOFs.
    """
    # idle is accepted purely for symmetry with the settings subprocess —
    # the HUD has no separate eager/idle mode (it's always "ready and
    # waiting for commands"), so the flag's only effect is to make the
    # invocation legible in `ps`.
    _ = idle

    cfg = load_config()
    _setup_logging("INFO", debug=cfg.debug)
    # File logging + excepthooks mirror what `service()` does. Without
    # these, every `[hud] ...` line and any startup crash traceback
    # vanishes — the HUD subprocess inherits stderr from a windowed
    # PyInstaller exe, which is /dev/null. Critical for triaging
    # "I can't see the HUD" reports: the subprocess's own
    # window-position + screen-detection logs land in agent.log
    # alongside the parent's `[hud] spawning subprocess` line.
    _setup_file_logging(cfg.logs_dir, cfg.log_level, cfg.debug)
    _install_excepthooks()
    log = logging.getLogger("hud")

    from .gui.hud.window import HudWindow

    try:
        HudWindow(cfg, demo=demo).run_blocking()
    except Exception:
        log.exception("HUD window crashed")


if __name__ == "__main__":
    cli()
