"""Configuration loaded from ~/.eloquy/agent/config.toml with env overrides."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    # User-level directory under the home folder.  A background service has no
    # meaningful cwd, so home-based is the only sane default.
    # Override with ELOQUY_DATA_DIR for a different location.
    return Path.home() / ".eloquy" / "agent"


class CaptureConfig(BaseSettings):
    sample_rate: int = 16000
    frame_ms: int = 20
    mic_device: str | None = None  # None = default input
    sys_device: str | None = None  # None = default loopback


class VADConfig(BaseSettings):
    threshold: float = 0.5
    min_speech_ms: int = 200
    hangover_ms: int = 300


class ConversationConfig(BaseSettings):
    joint_silence_close_secs: float = 45.0
    max_session_secs: float = 3600.0
    min_user_turn_secs: float = 8.0
    min_user_total_secs: float = 15.0
    min_user_turns_for_total: int = 2
    min_sys_voiced_secs: float = 1.0
    # Density-based STT: when mic_total / elapsed < stt_full_density, transcribe
    # the system stream only in ±stt_context_pad_secs windows around mic VAD
    # segments. Cuts STT cost on passive-media-with-occasional-talk sessions
    # without changing any discard logic (LLM still judges).
    stt_full_density: float = 0.05
    stt_context_pad_secs: float = 60.0
    # Pad around each VAD segment (mic or system) when building the final
    # saved audio. Regions outside any padded segment are zero-filled so
    # dead air + static artifacts don't end up in the on-disk capture. Small
    # pad keeps speech starts/ends from being clipped.
    final_audio_speech_pad_secs: float = 0.5
    # Before zero-filling dead air, merge any two VAD segments whose gap is
    # shorter than this. Preserves conversational pauses (response latency,
    # thinking beats, intra-turn hesitation) as real audio — those pauses
    # are coachable signal for speech analysis. True dead air longer than
    # this threshold still gets zeroed. Set to 0 to disable merging and fall
    # back to strict per-segment trimming.
    final_audio_merge_gap_secs: float = 5.0
    # Pre-session rolling PCM buffer. Silero only yields a SpeechSegment after
    # the speech *ends* (hangover_ms of trailing silence), so by the time
    # on_segment fires and opens a session, the actual voiced audio happened
    # up to tens of seconds ago. We backfill it from this buffer on open so
    # the first turn of every session isn't silently truncated. 120s covers
    # any realistic uninterrupted opening utterance; at 16 kHz mono that's
    # ~48 MB per source at 25 min / 16 kHz mono int16.
    max_pre_buffer_secs: float = 1500.0


class STTConfig(BaseSettings):
    model: str = "small"
    compute_type: str = "int8"
    device: str = "cpu"
    # Force English transcription. Eloquy is an English coaching platform, so
    # auto-detection is both unnecessary and actively harmful — Whisper-small
    # frequently misidentifies accented-but-correct English as Tagalog/Malay/
    # Indonesian and then "transcribes" nonsense. Set to None to re-enable
    # auto-detect if you ever need multilingual support.
    language: str | None = "en"
    # If the mic stream's detected language is confidently non-English
    # (prob >= this threshold), discard the whole session before STT. Guards
    # against spending CPU on sessions where the user was clearly speaking
    # another language (and Whisper would hallucinate English for). Set to
    # 1.0 to disable the discard path. Default 0.85 = "really sure" only.
    non_english_discard_prob: float = 0.85


class SpeakerConfig(BaseSettings):
    threshold: float = 0.70
    max_other_speakers: int = 4


class LLMConfig(BaseSettings):
    repo_id: str = "Qwen/Qwen2.5-3B-Instruct-GGUF"
    filename: str = "qwen2.5-3b-instruct-q4_k_m.gguf"
    n_ctx: int = 8192
    n_threads: int | None = None
    idle_unload_secs: float = 300.0


class AuthConfig(BaseSettings):
    auth_url: str = "https://eloquy.threadlify.io/api/auth"
    client_id: str = "eloquy-desktop"  # Public OAuth client ID (no secret — PKCE)
    server_url: str = "https://eloquy.threadlify.io"
    scopes: str = "offline_access upload"
    redirect_port: int = 17223  # Preferred port for PKCE localhost redirect
    login_timeout_secs: int = 120

    @property
    def effective_server_url(self) -> str:
        """Base URL for API calls. Falls back to deriving from auth_url."""
        if self.server_url:
            return self.server_url.rstrip("/")
        if self.auth_url:
            # "http://localhost:3000/api/auth" -> "http://localhost:3000"
            from urllib.parse import urlparse
            parsed = urlparse(self.auth_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        return ""


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ELOQUY_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    log_level: str = "INFO"
    debug: bool = False
    # Periodic "still alive" status line emitted by the main loop so the user
    # can see at a glance whether the agent is idle, in a session, etc. Set
    # to 0 to disable. Override via ELOQUY_HEARTBEAT_SECS.
    heartbeat_secs: float = 30.0

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def auth_path(self) -> Path:
        return self.data_dir / "auth.json"

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "agent.pid"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    cfg = Config()
    cfg.ensure_dirs()
    return cfg
