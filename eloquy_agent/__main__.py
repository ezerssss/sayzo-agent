"""CLI entrypoint for the Eloquy local listening agent."""
from __future__ import annotations

import asyncio
import logging
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


async def _do_login(cfg, no_browser: bool = False) -> None:
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
            click.echo("Browser login unavailable, falling back to device code...")
            tokens = await device_code_flow(server, timeout_secs=cfg.auth.login_timeout_secs)

    store.save(tokens)
    click.echo("Login successful.")


@click.group()
def cli() -> None:
    """Eloquy local listening agent."""


@cli.command()
def setup() -> None:
    """Download all required model weights (idempotent)."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("setup")

    from huggingface_hub import hf_hub_download

    # Whisper — faster-whisper downloads on first transcribe, but we can pre-warm
    log.info("Whisper model %s will be fetched lazily on first transcription.", cfg.stt.model)

    # Qwen GGUF
    log.info("Downloading LLM weights: %s / %s", cfg.llm.repo_id, cfg.llm.filename)
    path = hf_hub_download(
        repo_id=cfg.llm.repo_id,
        filename=cfg.llm.filename,
        local_dir=str(cfg.models_dir),
        local_dir_use_symlinks=False,
    )
    log.info("LLM ready at %s", path)

    # First-run wizard: if not authenticated yet, run login → enroll.
    from .auth.store import TokenStore

    store = TokenStore(cfg.auth_path)
    if not store.has_tokens():
        click.echo()
        click.echo("Welcome to Eloquy! Let's get you set up.")
        click.echo()
        if cfg.auth.auth_url and cfg.auth.client_id:
            click.echo("Step 1/2: Log in to your Eloquy account.")
            asyncio.run(_do_login(cfg))
        else:
            click.echo("Auth not configured — skipping login. Set ELOQUY_AUTH__AUTH_URL and ELOQUY_AUTH__CLIENT_ID.")

        if not cfg.voiceprint_path.exists():
            click.echo()
            click.echo("Step 2/2: Record a voice sample so we can identify you.")
            ctx = click.get_current_context()
            ctx.invoke(enroll)
        click.echo()

    log.info("Setup complete.")


@cli.command()
@click.option("--seconds", default=10, help="Recording duration for enrollment.")
def enroll(seconds: int) -> None:
    """Record a voice sample to build the user's voiceprint."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("enroll")

    import numpy as np
    import sounddevice as sd

    log.info("Recording %d seconds — please speak naturally...", seconds)
    audio = sd.rec(
        int(seconds * cfg.capture.sample_rate),
        samplerate=cfg.capture.sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    log.info("Recording done. Computing embedding...")
    from .speaker import SpeakerIdentifier
    sp = SpeakerIdentifier(cfg.speaker, cfg.voiceprint_path)
    sp.enroll(audio[:, 0] if audio.ndim == 2 else audio)
    log.info("Enrollment complete.")


@cli.command()
def devices() -> None:
    """List available mic and loopback devices."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    import sounddevice as sd
    import pyaudiowpatch as pyaudio

    click.echo("--- sounddevice (input) ---")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            click.echo(f"  [{i}] {d['name']} (in={d['max_input_channels']})")

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


@cli.command("test-capture")
@click.option("--seconds", default=10)
@click.option("--dump-wav", is_flag=True, help="Save captured mic/system audio as WAV files for inspection.")
def test_capture(seconds: int, dump_wav: bool) -> None:
    """Capture mic + system audio for N seconds and report frame counts."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)

    async def _run() -> None:
        from .capture.mic import MicCapture
        from .capture.system import SystemCapture

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


@cli.command()
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


@cli.command()
def run() -> None:
    """Run the listening agent (foreground, verbose terminal output)."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("run")

    from .auth.store import TokenStore

    store = TokenStore(cfg.auth_path)
    if not store.has_tokens():
        log.warning("Not authenticated. Run `eloquy-agent login` to enable uploads.")

    from .app import Agent

    agent = Agent(cfg)

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


if __name__ == "__main__":
    cli()
