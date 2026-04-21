"""Configuration loaded from ~/.sayzo/agent/config.toml with env overrides."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    # User-level directory under the home folder.  A background service has no
    # meaningful cwd, so home-based is the only sane default.
    # Override with SAYZO_DATA_DIR for a different location.
    return Path.home() / ".sayzo" / "agent"


class CaptureConfig(BaseSettings):
    sample_rate: int = 16000
    frame_ms: int = 20
    mic_device: str | None = None  # None = default input
    sys_device: str | None = None  # None = default loopback

    # Opus encoder (see sink.encode_opus_stereo). `application=audio` is
    # libopus's general-purpose mode — it preserves high frequencies, stereo
    # imaging, and transients. We used to run `voip` for its speech-focused
    # bit allocation, but the speech-band filter it applies murders any
    # non-speech content on the system channel (music, game audio, video)
    # and the narrowband artifact leaks into mic-side ambient noise too.
    # At 96 kbps stereo, `audio` is transparent for speech and good enough
    # for music; playback sounds markedly better than the old 64 kbps voip
    # defaults at the cost of ~50% larger files.
    opus_bitrate: int = 96000
    opus_application: str = "audio"

    # Post-capture DSP (see dsp.py). Runs at session close, after
    # transcription + speaker embedding (both use the raw PCM upstream), so
    # these settings do not affect STT. `dsp_enabled=False` restores the
    # raw-PCM path byte-for-byte (except for the opus_application setting,
    # which is intrinsic to the encoder path).
    dsp_enabled: bool = True
    denoise_enabled: bool = True  # mic channel only
    # noisereduce prop_decrease, 0..1. At 0.85 the stationary spectral gate
    # aggressively suppresses anything it judges "noise-like" — great for
    # constant hum, but it introduces phasey/robotic artifacts whenever the
    # noise floor is non-stationary (typing, room tone, far-side chatter).
    # 0.5 keeps a clear improvement over raw noise without audible artifacts.
    denoise_strength: float = 0.5
    highpass_mic_hz: float = 80.0  # Butterworth HPF cutoff on mic, 0 = off
    highpass_sys_hz: float = 40.0  # lighter HPF on system (just kill DC/rumble)
    peak_normalize_dbfs: float = -1.0  # post-DSP peak norm target


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
    # Gap-fill thresholds for the mono-clock invariant. When a frame arrives
    # with capture_mono_ts more than this far past "expected next sample
    # time", the detector zero-fills the gap so buffer offsets stay aligned
    # with wall-clock. Too tight fires on normal scheduler jitter; too loose
    # lets real dropped frames shift the timeline.
    # Mic: sounddevice callbacks fire on ~20 ms cadence; 60 ms tolerates one
    # skipped wakeup before zero-filling.
    # System: WASAPI / audio-tap arrive in 500 ms batches; 150 ms tolerates
    # one scheduler hiccup on either end of a batch.
    gap_tolerance_secs_mic: float = 0.060
    gap_tolerance_secs_system: float = 0.150


class STTConfig(BaseSettings):
    model: str = "small"
    compute_type: str = "int8"
    device: str = "cpu"
    # Force English transcription. Sayzo is an English coaching platform, so
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
    auth_url: str = "https://sayzo.app/api/auth"
    client_id: str = "sayzo-desktop"  # Public OAuth client ID (no secret — PKCE)
    server_url: str = "https://sayzo.app"
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


class UploadConfig(BaseSettings):
    # Periodic retry-sweep cadence. The sweep scans captures_dir for records
    # whose next_attempt_at has passed and tries to upload them. Set <= 0 to
    # disable the periodic sweep (startup sweep still runs).
    retry_sweep_interval_secs: float = 900.0  # 15 min

    # Exponential-ish backoff for `transient` failures, in seconds. Each
    # successive failure waits the NEXT value; the final value is the
    # steady-state cap (applied with ±10% jitter to avoid burst stampedes).
    transient_backoff_secs: list[int] = [300, 900, 3600, 10800, 21600]

    # After this many `permanent_other` attempts a record becomes
    # `failed_permanent` and is never retried. `permanent_client` (4xx from
    # the server) is always terminal after 1 attempt.
    max_permanent_other_attempts: int = 3

    # When the server returns 402 credit_limit_reached, pause ALL uploads
    # (live + retry) for this long. Default 24h per spec — retrying sooner
    # just hammers the server when the user genuinely has no credits.
    credit_lockout_secs: float = 86400.0

    # How many records the retry sweep uploads per tick. Caps per-sweep work
    # so even a backlog of thousands doesn't drag a single sweep to an hour.
    # Startup sweep uses this same cap. Set <= 0 for unlimited.
    max_uploads_per_sweep: int = 20

    # How many concurrent uploads may run. Keep at 1 — the server is the
    # bottleneck and concurrent multi-minute opus uploads would blow out a
    # home uplink.
    max_concurrent_uploads: int = 1

    # Filename for the persisted global-pause sidecar, stored under
    # captures_dir. Atomic-written (temp + os.replace).
    pause_state_filename: str = ".upload_state.json"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAYZO_",
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
    # to 0 to disable. Override via SAYZO_HEARTBEAT_SECS.
    heartbeat_secs: float = 30.0
    # Native desktop toast after each kept session. SAYZO_NOTIFICATIONS_ENABLED=0 to disable.
    notifications_enabled: bool = True

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)

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
