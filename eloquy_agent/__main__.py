"""CLI entrypoint for the Eloquy local listening agent."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time

import click

from .config import load_config


def _setup_logging(level: str, debug: bool) -> None:
    lvl = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


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
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(handler)


async def _do_login(cfg, no_browser: bool = False, quiet: bool = False) -> None:
    """Run the login flow (PKCE primary, device code fallback)."""
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
            )
        except PKCEUnavailable:
            if not quiet:
                click.echo("Browser login unavailable, falling back to device code...")
            tokens = await device_code_flow(server, timeout_secs=cfg.auth.login_timeout_secs)

    store.save(tokens)
    if not quiet:
        click.echo("Login successful.")


@click.group()
def cli() -> None:
    """Eloquy local listening agent."""


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
        click.echo("  ScreenCaptureKit captures all system audio output.")
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
        sys_ = SystemCapture(cfg.capture.sample_rate, cfg.capture.frame_ms, cfg.capture.sys_device)
        await mic.start()
        await sys_.start()
        mic_n = sys_n = 0
        mic_frames = []
        sys_frames = []
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                frame = await asyncio.wait_for(mic.queue.get(), timeout=0.05)
                mic_n += 1
                if dump_wav:
                    mic_frames.append(frame)
            except asyncio.TimeoutError:
                pass
            try:
                frame = await asyncio.wait_for(sys_.queue.get(), timeout=0.05)
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
        eloquy-agent replay conversation.wav
        eloquy-agent replay conversation.wav --speed 4
        eloquy-agent replay conversation.wav --channel mic
        eloquy-agent replay call.mp3 --channel system --speed 0
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
    """Authenticate with the Eloquy server."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    if not cfg.auth.auth_url or not cfg.auth.client_id:
        click.echo("Auth not configured. Set ELOQUY_AUTH__AUTH_URL and ELOQUY_AUTH__CLIENT_ID.")
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
    console.print("[bold cyan]  Eloquy Agent Setup[/]")
    console.print("[cyan]  ==================[/]")
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
            console.print("  Run [bold]eloquy-agent first-run[/] again to retry.")
            sys.exit(1)

    console.print()

    # Step 2: Login
    from .auth.store import TokenStore

    store = TokenStore(cfg.auth_path)
    if store.has_tokens():
        console.print("  [green]Already logged in.[/]")
    elif cfg.auth.auth_url and cfg.auth.client_id:
        console.print("  Your browser will open to log in to Eloquy.")
        console.print()
        for i in range(3, 0, -1):
            console.print(f"  Opening browser in [bold]{i}[/]...", end="\r")
            time.sleep(1)
        console.print("  Opening browser...           ")
        console.print()

        # Suppress noisy HTTP/auth logs during login.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("eloquy_agent.auth").setLevel(logging.WARNING)

        try:
            asyncio.run(_do_login(cfg, quiet=True))
            console.print("  [green]Login successful.[/]")
        except KeyboardInterrupt:
            console.print("\n  [yellow]Cancelled.[/]")
            sys.exit(130)
        except Exception as e:
            console.print(f"  [yellow]Login skipped: {e}[/]")
            console.print("  You can log in later with: [bold]eloquy-agent login[/]")
    else:
        console.print("  [dim]Auth not configured — skipping login.[/]")

    # Step 3: Start the service in the background (if not already running)
    from .pidfile import is_running

    console.print()
    if is_running(cfg.pid_path):
        console.print("  [green]Eloquy Agent is already running.[/]")
    else:
        console.print("  Starting Eloquy Agent...")
        import subprocess
        from pathlib import Path
        exe = sys.executable
        popen_kwargs = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Detach from the installer shell so closing Terminal doesn't SIGHUP the service.
            popen_kwargs["start_new_session"] = True
        if getattr(sys, "frozen", False):
            # On Windows, prefer the sibling windowless service exe so no
            # console window appears in the background.
            if sys.platform == "win32":
                service_exe = Path(exe).parent / "eloquy-agent-service.exe"
                if service_exe.exists():
                    exe = str(service_exe)
            subprocess.Popen([exe, "service"], **popen_kwargs)
        else:
            subprocess.Popen([exe, "-m", "eloquy_agent", "service"], **popen_kwargs)
        console.print("  [green]Eloquy Agent is now running in the background.[/]")

    console.print()
    console.print("  [bold green]Setup complete![/]")
    console.print("  The agent will start automatically on login.")
    console.print()


@cli.command()
def run() -> None:
    """Run the listening agent (foreground, verbose terminal output)."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("run")

    from .auth.store import TokenStore

    upload_client = None
    store = TokenStore(cfg.auth_path)
    if not store.has_tokens():
        log.warning("Not authenticated. Run `eloquy-agent login` to enable uploads.")
    elif cfg.auth.effective_server_url:
        from .auth.client import AuthenticatedClient
        from .auth.server import HttpAuthServer
        from .upload import AuthenticatedUploadClient

        auth_server = HttpAuthServer(cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes)
        store = TokenStore(cfg.auth_path, auth_server=auth_server)
        client = AuthenticatedClient(cfg.auth.effective_server_url, store)
        upload_client = AuthenticatedUploadClient(client, cfg.captures_dir)
        log.info("Uploads enabled → %s", cfg.auth.effective_server_url)

    from .app import Agent

    agent = Agent(cfg, upload_client=upload_client)

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
def service() -> None:
    """Run the agent as a background service (no terminal output, file logging)."""
    cfg = load_config()
    _setup_file_logging(cfg.logs_dir)
    log = logging.getLogger("service")

    from .pidfile import is_running, write_pid, remove_pid

    if is_running(cfg.pid_path):
        log.warning("service already running, exiting")
        return

    write_pid(cfg.pid_path)
    log.warning("eloquy-agent service starting (pid=%d)", os.getpid())

    from .auth.store import TokenStore
    from .gui.tray import TrayIcon, TrayState, Status

    upload_client = None
    store = TokenStore(cfg.auth_path)
    if store.has_tokens() and cfg.auth.effective_server_url:
        from .auth.client import AuthenticatedClient
        from .auth.server import HttpAuthServer
        from .upload import AuthenticatedUploadClient

        auth_server = HttpAuthServer(cfg.auth.auth_url, cfg.auth.client_id, cfg.auth.scopes)
        store = TokenStore(cfg.auth_path, auth_server=auth_server)
        client = AuthenticatedClient(cfg.auth.effective_server_url, store)
        upload_client = AuthenticatedUploadClient(client, cfg.captures_dir)
        log.warning("uploads enabled → %s", cfg.auth.effective_server_url)

    from .app import Agent

    tray_state = TrayState()
    tray = TrayIcon(tray_state, cfg.captures_dir)
    tray.start()

    agent = Agent(cfg, upload_client=upload_client)

    async def _main() -> None:
        loop = asyncio.get_running_loop()

        def _handle_stop() -> None:
            log.warning("shutdown requested")
            tray.stop()
            agent.stop()

        try:
            loop.add_signal_handler(signal.SIGINT, _handle_stop)
            loop.add_signal_handler(signal.SIGTERM, _handle_stop)
        except NotImplementedError:
            pass

        # Windows: Task Scheduler sends SIGBREAK (Ctrl+Break) on stop.
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, lambda *_: _handle_stop())

        # Poll the tray thread for pause/resume/quit signals.
        async def _tray_bridge() -> None:
            was_paused = False
            while not agent._stop.is_set():
                await asyncio.sleep(0.5)
                if tray_state.quit_event.is_set():
                    _handle_stop()
                    return
                paused = tray_state.pause_event.is_set()
                if paused and not was_paused:
                    agent.pause()
                elif not paused and was_paused:
                    agent.resume()
                was_paused = paused
                tray.update()

        asyncio.create_task(_tray_bridge())
        await agent.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        tray.stop()
        remove_pid(cfg.pid_path)
        log.warning("eloquy-agent service stopped")


if __name__ == "__main__":
    cli()
