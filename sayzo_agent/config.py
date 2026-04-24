"""Configuration loaded from ~/.sayzo/agent/config.toml with env overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

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

    # v1.7.0: per-app system-audio scoping. When the agent arms for a
    # specific app (whitelist consent, or hotkey smart-guess identified
    # a mic-holder), the system-audio capture scopes to just that app's
    # PIDs via WASAPI process loopback on Windows / CoreAudio process tap
    # include-list on macOS. Prevents Spotify / YouTube bleeding into a
    # Zoom capture.
    #
    # - ``arm_app`` (default): scope to the armed app's PIDs when known,
    #   fall back to endpoint-wide loopback otherwise (older OS builds,
    #   activation failures, hotkey with no mic-holder).
    # - ``endpoint``: always use endpoint-wide loopback, like pre-v1.7.0.
    #   Safety valve for users on unusual configurations where per-app
    #   capture misbehaves — ``SAYZO_CAPTURE__SYSTEM_SCOPE=endpoint``.
    system_scope: Literal["arm_app", "endpoint"] = "arm_app"

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
    # max_session_secs was the 60-min safety cap used in the always-on model.
    # Removed in the armed model: the user explicitly armed, can hotkey-stop,
    # and the long-meeting check-in toast (see ArmConfig) is the backstop for
    # unattended long sessions.
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


class EchoGuardConfig(BaseSettings):
    """Per-segment classifier that drops speaker-to-mic bleed (echo) before
    the substantive-user-turn gate and STT. See
    ~/.claude/plans/we-have-a-big-twinkling-wilkes.md for design.

    Biased toward keeping user speech — the prior enrollment-based approach
    was reverted for false-positive drops. AND of two tests (coherence high
    AND residual has no speech) is required to drop a segment.
    """
    enabled: bool = True

    # Cheap skips
    min_system_rms: float = 0.005  # float32 RMS, ~-46 dBFS
    min_mic_rms_for_test: float = 0.002
    min_xcorr_peak: float = 0.15  # normalized cross-correlation peak

    # Classification thresholds (both must fire to drop).
    # Tuned for server-side channel-based diarization (Deepgram): any echo
    # residue on the mic channel is attributed to the user by Deepgram even
    # at low amplitude, so we bias more aggressively toward dropping. Real
    # meeting users speak at -20 to -10 dBFS, producing residual-speech
    # probabilities near 1.0, well above the keep threshold.
    coh_high_threshold: float = 0.50  # mean weighted speech-band coherence
    residual_speech_keep_prob: float = 0.25  # Silero max chunk prob → keep

    # Delay / FFT sizing
    xcorr_search_ms: int = 200  # ± delay window for alignment
    fft_window_samples: int = 2048  # ~128 ms Welch nperseg
    speech_band_lo_hz: float = 300.0
    speech_band_hi_hz: float = 3400.0

    # Subdivision catches Silero segments that merge echo + user into one VAD
    # segment (and reiteration cases where user echoes a phrase after a short
    # pause). Fine hop catches short echo fragments inside longer merged
    # segments — the "in today a" / "few seconds" class of leaks.
    subdivide_long_segments_secs: float = 4.0  # 0 disables
    subdivide_window_secs: float = 1.0
    subdivide_hop_secs: float = 0.25

    # Cosine fade at zero'd-region boundaries so Whisper's log-mel frontend
    # doesn't see a spectral cliff at echo → user transitions.
    taper_ms: float = 5.0

    # Opt-in: dump per-dropped-segment WAVs (mic + sys + residual) under
    # <data_dir>/logs/echo_debug/<session_id>/ for offline inspection.
    debug: bool = False


class LLMConfig(BaseSettings):
    repo_id: str = "Qwen/Qwen2.5-3B-Instruct-GGUF"
    filename: str = "qwen2.5-3b-instruct-q4_k_m.gguf"
    n_ctx: int = 8192
    n_threads: int | None = None
    idle_unload_secs: float = 300.0


class DetectorSpec(BaseSettings):
    """Per-app detection rule for the whitelist auto-suggest path.

    See :mod:`sayzo_agent.arm.detectors` for matching logic.

    - ``app_key`` is a stable identity used for cooldown bucketing across
      sessions.
    - ``display_name`` is the user-facing label interpolated into consent /
      meeting-ended toasts.
    - ``process_names`` lists Windows executable names (e.g. ``zoom.exe``)
      that, when found holding an active capture session, match the rule.
    - ``bundle_ids`` lists macOS bundle identifiers (e.g. ``us.zoom.xos``)
      treated equivalently on macOS. Mac can't attribute mic-in-use to a
      specific process cheaply, so a running+recently-foreground check is
      used instead (see ``arm/platform_mac.py``).
    - ``is_browser`` marks rules that only trigger when the browser process
      holds the mic AND one of ``url_patterns`` matches the active tab.
      This is how Google Meet / Teams web / Zoom web / Webex / Whereby /
      Jitsi / 8x8 are matched.
    - ``title_patterns`` are tried against any visible browser window title
      when ``is_browser`` is True. Needed on Windows where we can't cheaply
      read the active-tab URL; the Chrome/Edge window title (e.g. ``Meet -
      abc-defg-hij - Google Chrome``) is the only signal available.
    - ``disabled`` marks a spec the user has toggled off in the Settings
      Meeting Apps pane. The matcher skips these so the consent toast
      doesn't fire — but the spec stays in the list so the user can flip
      it back on without losing any custom URL patterns / process names.
    """

    app_key: str
    display_name: str
    process_names: list[str] = Field(default_factory=list)
    bundle_ids: list[str] = Field(default_factory=list)
    is_browser: bool = False
    url_patterns: list[str] = Field(default_factory=list)
    title_patterns: list[str] = Field(default_factory=list)
    disabled: bool = False


def default_detector_specs() -> list[DetectorSpec]:
    """Ship-with whitelist. Users edit via Settings → Meeting Apps (which
    writes the list to ``user_settings.json`` under ``arm.detectors``),
    or override wholesale via ``SAYZO_ARM__DETECTORS``."""
    return [
        # Desktop meeting apps — detected by process name (Win) or bundle id (Mac)
        # holding an active capture session / mic running system-wide.
        DetectorSpec(
            app_key="zoom", display_name="Zoom",
            process_names=["zoom.exe", "CptHost.exe"],
            bundle_ids=["us.zoom.xos"],
        ),
        DetectorSpec(
            app_key="teams_desktop", display_name="Microsoft Teams",
            process_names=["ms-teams.exe", "Teams.exe"],
            bundle_ids=["com.microsoft.teams", "com.microsoft.teams2"],
        ),
        DetectorSpec(
            app_key="discord", display_name="Discord",
            process_names=["Discord.exe"],
            bundle_ids=["com.hnc.Discord"],
        ),
        DetectorSpec(
            app_key="slack", display_name="Slack",
            process_names=["slack.exe"],
            bundle_ids=["com.tinyspeck.slackmacgap"],
        ),
        DetectorSpec(
            app_key="webex", display_name="Webex",
            process_names=["webex.exe", "CiscoCollabHost.exe"],
            bundle_ids=["Cisco-Systems.Spark", "com.webex.meetingmanager"],
        ),
        DetectorSpec(
            app_key="skype", display_name="Skype",
            process_names=["Skype.exe"],
            bundle_ids=["com.skype.skype"],
        ),
        DetectorSpec(
            app_key="facetime", display_name="FaceTime",
            bundle_ids=["com.apple.FaceTime"],  # macOS only
        ),
        DetectorSpec(
            app_key="whatsapp", display_name="WhatsApp",
            process_names=["WhatsApp.exe"],
            bundle_ids=["net.whatsapp.WhatsApp"],
        ),
        DetectorSpec(
            app_key="signal", display_name="Signal",
            process_names=["Signal.exe"],
            bundle_ids=["org.whispersystems.signal-desktop"],
        ),
        DetectorSpec(
            app_key="gotomeeting", display_name="GoTo",
            process_names=["g2mcomm.exe", "g2mlauncher.exe"],
            bundle_ids=["com.logmein.GoToMeeting"],
        ),
        DetectorSpec(
            app_key="bluejeans", display_name="BlueJeans",
            process_names=["BlueJeans.exe"],
            bundle_ids=["com.bluejeans.app"],
        ),
        DetectorSpec(
            app_key="chime", display_name="Amazon Chime",
            process_names=["Chime.exe"],
            bundle_ids=["com.amazon.Chime"],
        ),
        DetectorSpec(
            app_key="ringcentral", display_name="RingCentral",
            process_names=["RingCentral.exe", "RCMeetings.exe"],
            bundle_ids=["com.ringcentral.rcoffice"],
        ),
        DetectorSpec(
            app_key="dialpad", display_name="Dialpad",
            process_names=["Dialpad.exe"],
            bundle_ids=["co.dialpad.dialpad"],
        ),

        # Web meeting platforms — browser holds the mic AND URL or title
        # matches. Title patterns are a Windows fallback (no cheap tab-URL
        # read) and are deliberately specific enough to avoid matching a
        # logged-out landing page.
        DetectorSpec(
            app_key="gmeet", display_name="Google Meet", is_browser=True,
            url_patterns=[r"^https://meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}"],
            title_patterns=[r"\bMeet - [a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}\b"],
        ),
        DetectorSpec(
            app_key="teams_web", display_name="Microsoft Teams",
            is_browser=True,
            url_patterns=[
                r"teams\.microsoft\.com/.+/l/meetup-join/",
                r"teams\.microsoft\.com/_#/conversations/.+/meeting",
            ],
        ),
        DetectorSpec(
            app_key="zoom_web", display_name="Zoom", is_browser=True,
            url_patterns=[
                r"^https://[^/]+\.zoom\.us/wc/join/",
                r"^https://[^/]+\.zoom\.us/j/\d+",
            ],
            # Zoom web titles the meeting window "Zoom Meeting"; distinct
            # enough from their marketing pages / client downloads.
            title_patterns=[r"\bZoom Meeting\b"],
        ),
        DetectorSpec(
            app_key="webex_web", display_name="Webex", is_browser=True,
            url_patterns=[r"^https://[^/]+\.webex\.com/(meet|wbxmjs|webappng)/"],
        ),
        DetectorSpec(
            app_key="whereby", display_name="Whereby", is_browser=True,
            url_patterns=[r"^https://whereby\.com/[^/]+"],
        ),
        DetectorSpec(
            app_key="jitsi", display_name="Jitsi Meet", is_browser=True,
            url_patterns=[r"^https://meet\.jit\.si/[^/]+"],
        ),
        DetectorSpec(
            app_key="8x8", display_name="8x8 Meet", is_browser=True,
            url_patterns=[r"^https://8x8\.vc/[^/]+"],
        ),
    ]


class ArmConfig(BaseSettings):
    """Configuration for the armed-only capture model.

    See ``~/.claude/plans/so-right-now-what-snug-corbato.md`` for the full
    design. The armed model replaces always-listening: audio streams only
    open after an explicit arm signal (hotkey or whitelist consent).
    """

    # Global hotkey binding. Users can change this via the Settings GUI (see
    # sayzo_agent.gui.settings_window) or the onboarding walkthrough. Values
    # are written to data_dir/user_settings.json and overlaid onto this
    # default at load time; SAYZO_ARM__HOTKEY env var still wins.
    hotkey: str = "ctrl+alt+s"

    # How often the whitelist watcher polls foreground + mic-holders while
    # disarmed, and the meeting-ended watcher polls while armed.
    poll_interval_secs: float = 2.0

    # Whitelist consent toast timings.
    consent_toast_timeout_secs: float = 30.0

    # Hotkey confirmation toast timings (both start + stop).
    hotkey_confirm_timeout_secs: float = 10.0

    # End-of-meeting confirmation toast (fires when detector enters
    # PENDING_CLOSE on joint silence).
    end_toast_timeout_secs: float = 15.0

    # Whitelist-consent suppression, keyed by app_key.
    #
    # After a natural session close (user was armed, detector closed the
    # session), we apply a short timed cooldown so we don't immediately
    # re-prompt for the same app if it's still holding the mic.
    cooldown_after_session_secs: float = 600.0   # 10 min after session naturally ended
    #
    # After the user declines or ignores the consent toast, we suppress
    # new toasts for that app_key until we observe the app release the
    # mic continuously for this long. This matches user intent better
    # than a flat time cooldown: "Not now" means "not this meeting", so
    # leaving + rejoining (a new session) fires a fresh prompt, while
    # staying in the declined meeting stays quiet — the hotkey is always
    # available as an opt-in path during the suppressed window.
    decline_release_grace_secs: float = 15.0

    # Long-meeting check-in: elapsed-session marks (seconds from session
    # open) at which to fire the "still in the meeting?" toast. Hourly until
    # 2h, then every 30 min. Extends indefinitely with 30-min steps.
    long_meeting_checkin_marks_secs: list[float] = Field(
        default_factory=lambda: [3600, 7200, 9000, 10800, 12600, 14400, 16200, 18000]
    )
    checkin_toast_timeout_secs: float = 15.0

    # Meeting-ended watcher (whitelist-armed sessions only).
    # How long the arm-app can be absent from mic-holders before the toast
    # fires. Absorbs transient drops / immediate-rejoin scenarios.
    whitelist_arm_release_grace_secs: float = 15.0
    meeting_ended_toast_timeout_secs: float = 15.0
    # After "Keep going", snooze the watcher this long before re-asking.
    # Each snooze-expiry fires a fresh toast; non-response on that toast
    # defaults to Wrap up and commits the close.
    meeting_ended_snooze_secs: float = 600.0

    # Per-app detection rules. Populated from ``default_detector_specs()``.
    detectors: list[DetectorSpec] = Field(default_factory=default_detector_specs)

    # Sub-toggle for the "Sayzo is capturing — Press X to stop" toast fired
    # after every successful arm. Exposed in the Settings window. Master
    # ``Config.notifications_enabled`` still wins; consent + end-of-meeting
    # toasts are unaffected (they're how the user decides what to capture).
    notify_post_arm: bool = True


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
    # Master toggle — when False, no non-consent toasts fire. Consent +
    # end-of-meeting toasts still fire (they're how the user decides what
    # gets captured). SAYZO_NOTIFICATIONS_ENABLED=0 to disable.
    notifications_enabled: bool = True
    # Sub-toggles under the master. Exposed in the Settings window's
    # Notifications pane; persisted to user_settings.json. See also
    # ``ArmConfig.notify_post_arm`` for the arm-time sub-toggle.
    notify_welcome: bool = True
    notify_capture_saved: bool = True

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    echo_guard: EchoGuardConfig = Field(default_factory=EchoGuardConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    arm: ArmConfig = Field(default_factory=ArmConfig)
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
    """Build a Config with layered overrides.

    Load order, lowest to highest precedence:
      1. Pydantic field defaults.
      2. ``data_dir/user_settings.json`` (written by the Settings GUI /
         onboarding; survives restarts).
      3. ``SAYZO_*`` environment variables (dev overrides — still win).

    Pydantic-settings puts init kwargs above env vars by default, so we
    explicitly filter out any user-settings field for which a matching
    ``SAYZO_*__*`` env var is present. The env var then flows through the
    normal source chain and wins.
    """
    # First resolve the data_dir so we know where to look for user settings.
    # Env var SAYZO_DATA_DIR still wins; we need a Config instance to know
    # the effective value.
    probe = Config()
    data_dir = probe.data_dir

    from . import settings_store
    user = settings_store.load(data_dir)

    # Build nested init kwargs, dropping any leaf that env already overrode.
    init_kwargs: dict = {}

    # Top-level scalar fields (notifications master + sub-toggles, etc.).
    # Filter by env — only the non-nested SAYZO_* vars count here.
    env_top_keys = {
        k[len("SAYZO_"):].lower()
        for k in os.environ
        if k.upper().startswith("SAYZO_") and "__" not in k[len("SAYZO_"):]
    }
    for key in ("notifications_enabled", "notify_welcome", "notify_capture_saved"):
        if key in user and key not in env_top_keys:
            init_kwargs[key] = user[key]

    if isinstance(user.get("arm"), dict):
        env_arm_keys = {
            k[len("SAYZO_ARM__"):].lower()
            for k in os.environ
            if k.upper().startswith("SAYZO_ARM__")
        }
        user_arm = {k: v for k, v in user["arm"].items() if k.lower() not in env_arm_keys}
        if user_arm:
            init_kwargs["arm"] = {**probe.arm.model_dump(), **user_arm}

    cfg = Config(**init_kwargs) if init_kwargs else probe
    cfg.ensure_dirs()
    return cfg
