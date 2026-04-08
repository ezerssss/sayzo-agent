"""CLI entrypoint for the Eloquy local listening agent."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import click

from .config import load_config


def _setup_logging(level: str, debug: bool) -> None:
    lvl = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


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
    log.info("Setup complete. Next: `eloquy-agent enroll`")


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
    import soundcard as sc

    click.echo("--- sounddevice (input) ---")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            click.echo(f"  [{i}] {d['name']} (in={d['max_input_channels']})")
    click.echo("\n--- soundcard speakers (loopback) ---")
    for spk in sc.all_speakers():
        click.echo(f"  {spk.name}")
    click.echo(f"\nDefault speaker: {sc.default_speaker().name}")


@cli.command("test-capture")
@click.option("--seconds", default=10)
def test_capture(seconds: int) -> None:
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
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                await asyncio.wait_for(mic.queue.get(), timeout=0.05)
                mic_n += 1
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.wait_for(sys_.queue.get(), timeout=0.05)
                sys_n += 1
            except asyncio.TimeoutError:
                pass
        await mic.stop()
        await sys_.stop()
        click.echo(f"mic frames: {mic_n}  system frames: {sys_n}")

    asyncio.run(_run())


@cli.command()
def run() -> None:
    """Run the listening agent (foreground, verbose terminal output)."""
    cfg = load_config()
    _setup_logging(cfg.log_level, cfg.debug)
    log = logging.getLogger("run")

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
