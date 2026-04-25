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


def _setup_file_logging(logs_dir) -> None:
    """Configure rotating file-based logging for the background service."""
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
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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


@click.group()
@click.version_option(package_name="sayzo-agent")
def cli() -> None:
    """Sayzo local listening agent."""


@cli.command(hidden=True)
def devices() -> None:
    """List available mic and loopback devices."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    import sounddevice as sd

    click.echo("--- sounddevice (input) ---")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            click.echo(f"  [{i}] {d['name']} (in={d['max_input_channels']})")

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

        # Wait for all in-flight processing tasks (STT, LLM, sink) to finish.
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


@cli.command("first-run")
@click.pass_context
def first_run(ctx: click.Context) -> None:
    """One-time setup: download models and log in."""
    from rich.console import Console
    from huggingface_hub import hf_hub_download

    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    console = Console()

    # Let Ctrl+C kill the process immediately.
    ctx.resilient_parsing = True
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))

    console.print()
    console.print("[bold cyan]  Sayzo Agent Setup[/]")
    console.print("[cyan]  =================[/]")
    console.print()

    # Step 1: Download model
    # Suppress noisy HTTP and huggingface_hub logs during download.
    import warnings
    warnings.filterwarnings("ignore", message=".*unauthenticated.*")
    for noisy in ("httpx", "httpcore", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    model_path = cfg.models_dir / cfg.llm.filename
    if model_path.exists():
        console.print("  [green]Language model already downloaded.[/]")
    else:
        console.print("  Downloading language model...")
        try:
            hf_hub_download(
                repo_id=cfg.llm.repo_id,
                filename=cfg.llm.filename,
                local_dir=str(cfg.models_dir),
            )
            console.print("  [green]Language model ready.[/]")
        except KeyboardInterrupt:
            console.print("\n  [yellow]Cancelled.[/]")
            sys.exit(130)
        except Exception as e:
            console.print(f"  [red]Download failed: {e}[/]")
            console.print("  Run [bold]sayzo-agent first-run[/] again to retry.")
            sys.exit(1)

    console.print()

    # Step 2: Login
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

    # Step 3: Start the service in the background (if not already running)
    from pathlib import Path

    from .pidfile import is_running

    mac_plist = Path.home() / "Library/LaunchAgents/com.sayzo.agent.plist"

    console.print()
    if is_running(cfg.pid_path):
        console.print("  [green]Sayzo Agent is already running.[/]")
    elif sys.platform == "darwin" and mac_plist.exists():
        # launchd owns the service on installed macOS; the installer script
        # runs `launchctl load` immediately after first-run returns. Spawning
        # our own subprocess here would race it for the pidfile and leak the
        # detached service's stderr to the installer terminal.
        console.print("  [green]Sayzo Agent is configured to start automatically.[/]")
    else:
        console.print("  Starting Sayzo Agent...")
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
        console.print("  [green]Sayzo Agent is now running in the background.[/]")

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
    log = logging.getLogger("run")

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
    from .notify import APP_AUMID, DesktopNotifier, NoopNotifier

    notifier = DesktopNotifier(app_name=APP_AUMID) if cfg.notifications_enabled else NoopNotifier()
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

        await agent.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option(
    "--force-setup",
    is_flag=True,
    hidden=True,
    help="Always open the first-run GUI even if setup looks complete. "
    "Used by the NSIS finish-page-launch on Windows so users get visual "
    "confirmation right after install.",
)
def service(force_setup: bool) -> None:
    """Run the agent as a background service (no terminal output, file logging)."""
    cfg = load_config()
    _setup_file_logging(cfg.logs_dir)
    log = logging.getLogger("service")

    from .pidfile import is_running, write_pid, remove_pid

    if is_running(cfg.pid_path):
        log.warning("service already running, exiting")
        return

    write_pid(cfg.pid_path)
    from . import __version__
    log.warning("sayzo-agent service starting v%s (pid=%d)", __version__, os.getpid())

    # First-run gate. Detect missing setup signals (auth token, LLM weights,
    # macOS mic permission) and open the GUI setup window if any is missing
    # — or if the caller forced it via --force-setup (NSIS finish-page), or
    # if this is the very first .app launch on macOS (no marker file). The
    # window blocks the main thread until the user completes setup or
    # cancels. On cancel, exit cleanly without starting the tray + agent.
    from .gui.setup.detect import detect_setup
    from .gui.setup.marker import is_first_launch, mark_setup_seen

    setup_status = detect_setup(cfg)
    mac_first_launch = sys.platform == "darwin" and is_first_launch(cfg)
    should_show_gui = (
        force_setup or mac_first_launch or not setup_status.is_complete
    )
    log.warning(
        "first-run gate: force_setup=%s mac_first_launch=%s is_complete=%s "
        "(token=%s model=%s mic=%s onboarded=%s) → show_gui=%s",
        force_setup,
        mac_first_launch,
        setup_status.is_complete,
        setup_status.has_token,
        setup_status.has_model,
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
            return
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

    from .auth.store import TokenStore
    from .gui.tray import TrayIcon, TrayState, Status

    upload_client = None
    auth_client = None
    store = TokenStore(cfg.auth_path)
    if store.has_tokens() and cfg.auth.effective_server_url:
        from .auth.client import AuthenticatedClient
        from .auth.server import HttpAuthServer
        from .upload import AuthenticatedUploadClient

        auth_server = HttpAuthServer(cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes)
        store = TokenStore(cfg.auth_path, auth_server=auth_server)
        auth_client = AuthenticatedClient(cfg.auth.effective_server_url, store)
        upload_client = AuthenticatedUploadClient(auth_client, cfg.captures_dir)
        log.warning("uploads enabled → %s", cfg.auth.effective_server_url)

    from .app import Agent
    from .notify import APP_AUMID, DesktopNotifier, NoopNotifier

    tray_state = TrayState()
    tray = TrayIcon(tray_state, cfg.captures_dir)

    notifier = DesktopNotifier(app_name=APP_AUMID) if cfg.notifications_enabled else NoopNotifier()
    agent = Agent(cfg, upload_client=upload_client, notifier=notifier, auth_client=auth_client)

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
        from .gui.settings.ipc import IPCServer

        ipc_server = IPCServer(cfg.data_dir)
        ipc_server.register("ping", lambda: "pong")

        def _ipc_invalidate_token_cache() -> None:
            try:
                store.invalidate_cache()
            except Exception:
                log.debug("[ipc] invalidate_token_cache failed", exc_info=True)

        def _ipc_rebind_hotkey(binding: str) -> dict:
            err = agent.arm.rebind_hotkey(binding)
            return {"error": err}

        ipc_server.register("invalidate_token_cache", _ipc_invalidate_token_cache)
        ipc_server.register("rebind_hotkey", _ipc_rebind_hotkey)

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

        # One-at-a-time guard — a user double-clicking Settings... shouldn't
        # spin up two tk roots in parallel. tkinter's mainloop returns when
        # the window closes; the flag is cleared from the worker thread at
        # that point.
        settings_open = threading.Event()

        # Dedicated single-worker executor for the Settings tkinter window.
        # Tcl is thread-affine: opening Settings via the default asyncio
        # executor (32+ workers) means each invocation can land on a
        # different thread. Tcl tolerates being on ONE non-main thread, but
        # rotating across threads triggers ``Tcl_Panic`` (BREAKPOINT crash
        # 0x80000003 in tcl86t.dll, observed in field event-viewer logs on
        # v1.7.2). Pinning to one worker avoids that entirely — every
        # Settings open re-uses the same Tcl interpreter thread.
        from concurrent.futures import ThreadPoolExecutor as _SettingsExec
        settings_executor = _SettingsExec(
            max_workers=1, thread_name_prefix="sayzo-settings",
        )

        def _open_settings() -> None:
            try:
                from .gui.settings_window import open_settings_window

                open_settings_window(cfg, agent.arm)
            except Exception:
                log.warning("[tray] settings window crashed", exc_info=True)
            finally:
                settings_open.clear()

        def _use_pywebview_settings() -> bool:
            return os.environ.get("SAYZO_USE_PYWEBVIEW_SETTINGS", "0").lower() not in (
                "0", "", "false", "no", "off",
            )

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

        async def _spawn_settings_subprocess(open_flag: threading.Event) -> None:
            """Run ``sayzo-agent settings`` and clear the open-flag on exit.

            Errors here are non-fatal: the agent must keep ticking even if
            the Settings window failed to spawn (missing assets, OS-level
            spawn failure). The user can retry from the tray menu.
            """
            argv = _settings_subprocess_argv()
            log.info("[tray] spawning Settings subprocess: %s", argv)
            try:
                proc = await asyncio.create_subprocess_exec(*argv)
                await proc.wait()
                if proc.returncode != 0:
                    log.warning(
                        "[tray] settings subprocess exited with code %s",
                        proc.returncode,
                    )
            except Exception:
                log.warning("[tray] failed to spawn settings subprocess", exc_info=True)
            finally:
                open_flag.clear()

        def _sync_arm_state_to_tray() -> None:
            """Push ArmController.state → TrayState immediately.

            Called both from the bridge's 0.5 s poll and (synchronously, on
            the asyncio loop) from ArmController's state-change callback so
            menu labels reflect arm/disarm the moment the transition
            finishes — not up to 0.5 s later. Without this the user saw
            "right-click menu still says Start recording" right after
            arming.
            """
            cur = agent.arm.state
            tray_state.set_status(
                TrayStatus.ARMED if cur == ArmState.ARMED else TrayStatus.DISARMED
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
                # Settings... — opens the Settings window. Two dispatch paths:
                #
                #   * SAYZO_USE_PYWEBVIEW_SETTINGS=1 (default off during the
                #     migration): spawn the new pywebview Settings as a
                #     ``sayzo-agent settings`` subprocess. The subprocess
                #     owns its own main thread, which is what Cocoa needs on
                #     macOS — pystray holds the agent's main thread, so an
                #     in-process pywebview can't run there.
                #
                #   * Default: keep the legacy tkinter window on a dedicated
                #     single-worker executor so Tcl's thread affinity stays
                #     happy on Windows. Reentrancy guard prevents two roots.
                if tray_state.settings_event.is_set():
                    tray_state.settings_event.clear()
                    if not settings_open.is_set():
                        settings_open.set()
                        if _use_pywebview_settings():
                            asyncio.create_task(
                                _spawn_settings_subprocess(settings_open)
                            )
                        else:
                            loop.run_in_executor(settings_executor, _open_settings)
                # Belt-and-braces poll sync in case the callback ever fails
                # to fire (e.g. state changed before the callback was wired).
                _sync_arm_state_to_tray()
                cur_hotkey = agent.arm.current_hotkey
                if cur_hotkey != last_hotkey:
                    tray_state.set_hotkey_display(humanize_binding(cur_hotkey))
                    last_hotkey = cur_hotkey

        # Best-effort update check. Surfaces "Download Sayzo vX.Y.Z" in the
        # tray menu + fires ONE toast per newly-discovered version when the
        # public manifest at sayzo.app/releases/latest.json advertises
        # something newer than our installed __version__. Failures are logged
        # and swallowed — auto-update must never break capture. The two env
        # overrides exist so the E2E test in the plan's Verification section
        # can trigger a check in seconds instead of hours.
        async def _update_check() -> None:
            from . import __version__
            from .update import check
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

            notified_versions: set[str] = set()
            while not agent._stop.is_set():
                try:
                    info = await check(__version__)
                except Exception:
                    log.warning("[update] check failed", exc_info=True)
                    info = None

                if info is None:
                    tray_state.set_update_offer(None)
                else:
                    tray_state.set_update_offer(
                        UpdateOffer(version=info.version, url=info.url)
                    )
                    if info.version not in notified_versions:
                        notified_versions.add(info.version)
                        body = info.notes or f"v{info.version} is ready to install."
                        # Dispatch the toast on the heavy-worker executor to
                        # match the sink's invariant: on Windows, WinRT pins
                        # its COM apartment to whichever thread first builds
                        # the backend, so every notify() call must go through
                        # the same executor. See sink.py:530 + notify.py
                        # docstring for the full rationale.
                        try:
                            await loop.run_in_executor(
                                agent._executor, notifier.notify,
                                "Sayzo update available", body,
                            )
                        except Exception:
                            log.warning("[update] toast failed", exc_info=True)

                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

        asyncio.create_task(_tray_bridge())
        asyncio.create_task(_update_check())
        try:
            await agent.run()
        finally:
            await ipc_server.stop()

    try:
        if sys.platform == "darwin":
            # macOS: pystray uses AppKit, which requires NSStatusItem to be
            # instantiated on the main thread. Run asyncio on a worker thread
            # and hand the main thread to pystray.
            asyncio_exc: list[BaseException] = []

            def _asyncio_runner() -> None:
                try:
                    asyncio.run(_main())
                except BaseException as e:
                    asyncio_exc.append(e)
                finally:
                    tray.stop()

            worker = threading.Thread(target=_asyncio_runner, name="asyncio", daemon=False)
            worker.start()
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
                agent.stop()
                worker.join(timeout=5)
                log.warning("tray quit — killing process group and exiting")
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
            tray.start()
            asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
        try:
            settings_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        remove_pid(cfg.pid_path)
        log.warning("sayzo-agent service stopped")


@cli.command()
@click.option(
    "--pane",
    default=None,
    help="Open the Settings window with this pane selected (e.g. Account, About).",
)
def settings(pane: str | None) -> None:
    """Open the Settings window in a dedicated pywebview process.

    Spawned by the tray menu's "Settings…" click when
    ``SAYZO_USE_PYWEBVIEW_SETTINGS=1``. Exits 0 when the window closes;
    exits 0 immediately (with a log line) if another Settings window
    already holds the cross-process lock.
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
            SettingsWindow(cfg, pane=pane).run_blocking()
        except Exception:
            log.exception("settings window crashed")


if __name__ == "__main__":
    cli()
