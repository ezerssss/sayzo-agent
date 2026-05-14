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

    # System-audio scoping mode. Picks between capturing only the meeting
    # app's audio vs. all system audio output.
    #
    # - ``endpoint`` (default since v2.9.0): whole-system loopback —
    #   WASAPI loopback against the default render endpoint on Windows /
    #   global CoreAudio Process Tap on macOS. Matches what Granola /
    #   Krisp / most AI meeting note-takers do. Works reliably across
    #   Chrome versions, OS minor versions, and EDR-managed Macs. Cost:
    #   if you have Spotify or background apps playing during a meeting,
    #   their audio ends up in the capture too.
    # - ``arm_app`` (BETA, opt-in via Settings → Recording → "Per-app
    #   audio capture (Beta)" or ``SAYZO_CAPTURE__SYSTEM_SCOPE=arm_app``):
    #   scope to the armed app's PIDs via WASAPI process loopback / per-
    #   process Process Tap. Reduces background-app bleed, but per-PID
    #   attribution is fragile — Chrome's WebRTC renderer PID, audio-
    #   service helper PIDs spawned post-tap, and some EDR-managed macOS
    #   configurations all break it silently, producing empty captures
    #   the user only notices when their drills come back empty. The
    #   pre-2.9 default; demoted to beta after Sheen's Rippling Mac and
    #   other field reports made the failure pattern untenable.
    system_scope: Literal["arm_app", "endpoint"] = "endpoint"

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

    # Post-capture DSP (see dsp.py). Runs at session close on the raw
    # session PCM before Opus encoding — cleans up the on-disk audio
    # without affecting the server-side transcription pipeline.
    # `dsp_enabled=False` restores the raw-PCM path byte-for-byte (except
    # for the opus_application setting, which is intrinsic to the encoder
    # path).
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

    # Windows-only: opens a silent render stream on the same WASAPI endpoint
    # as the loopback capture, for the lifetime of an armed session. WASAPI
    # loopback documented behavior: when nothing is rendering on the endpoint,
    # the capture client receives NO packets (silence is skipped, not delivered
    # as zeros). ``stream.read()`` then blocks until something plays. This was
    # the root cause of intermittent ``sys_total=0.0s`` on hotkey-armed
    # sessions where the user played short bursts of audio with quiet stretches
    # between them. The pump's all-zeros render keeps the endpoint clock
    # ticking so the loopback delivers continuous frames (real audio +
    # actual silence). Industry-standard workaround — Microsoft's own sample
    # (Matthew van Eerde), NAudio docs, and ScreenRecorderLib all do it.
    # macOS uses CoreAudio Process Taps which deliver silence frames
    # continuously, so this flag is a no-op there.
    # ``SAYZO_CAPTURE__SYSTEM_SILENCE_PUMP_ENABLED=0`` disables if a driver
    # rejects the second stream open.
    system_silence_pump_enabled: bool = True


class VADConfig(BaseSettings):
    threshold: float = 0.5
    min_speech_ms: int = 200
    hangover_ms: int = 300


class ConversationConfig(BaseSettings):
    joint_silence_close_secs: float = 45.0
    min_user_turn_secs: float = 8.0
    min_user_total_secs: float = 10.0
    min_user_turns_for_total: int = 2
    min_sys_voiced_secs: float = 1.0
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
    # Hard upper bound on a single zero-fill gap. Real audio dropouts from
    # scheduler / driver / USB hiccups don't exceed a few hundred ms; any
    # bigger "gap" is stale state (stale frame from a previous arm cycle,
    # system suspend resume, mono clock skip). Re-anchor instead of filling
    # so the session can't end up minutes longer than the wall-clock event.
    max_gap_fill_secs: float = 2.0


class EchoGuardConfig(BaseSettings):
    """Per-segment classifier that drops speaker-to-mic bleed (echo) before
    the substantive-user-turn gate. See
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

    # Cosine fade at zero'd-region boundaries so the encoder doesn't see a
    # spectral cliff at echo → user transitions on the mic channel.
    taper_ms: float = 5.0

    # Opt-in: dump per-dropped-segment WAVs (mic + sys + residual) under
    # <data_dir>/logs/echo_debug/<session_id>/ for offline inspection.
    debug: bool = False


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
      treated equivalently on macOS. As of v2.5+ the matcher gets these
      from the ``audio-detect`` Swift helper which provides per-process
      attribution via Apple's responsibility SPI — same Pass 1
      behaviour as Windows, no foreground requirement (see
      ``arm/platform_mac.py``).
    - ``is_browser`` marks rules that only trigger when the browser process
      holds the mic AND one of ``url_patterns`` matches the active tab.
      This is how Google Meet / Teams web / Zoom web / Webex / Whereby /
      Jitsi / 8x8 are matched.
    - ``title_patterns`` are tried against any visible browser window title
      when ``is_browser`` is True. Each entry is an OR — any single match
      fires. Brand-name regexes work well in practice because the
      upstream gate (browser is foreground AND mic is active) supplies
      the precision; the title pattern only has to disambiguate "which
      meeting service?".
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
            # Brand-match (instead of meeting-code-match) because the
            # title format drifts across Chrome / Safari / Edge versions.
            # The mic-active + browser-foreground gate upstream supplies
            # the precision; this just disambiguates which service.
            title_patterns=[
                r"(?i)\bGoogle Meet\b",
                r"(?i)\bgmeet\b",
                # Legacy Chrome title format ("Meet - <code> - Google
                # Chrome") that some browser/OS combos still produce
                # without the "Google Meet" branding above.
                r"\bMeet - [a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}\b",
            ],
        ),
        DetectorSpec(
            app_key="teams_web", display_name="Microsoft Teams",
            is_browser=True,
            url_patterns=[
                r"teams\.microsoft\.com/.+/l/meetup-join/",
                r"teams\.microsoft\.com/_#/conversations/.+/meeting",
            ],
            title_patterns=[r"(?i)\bMicrosoft Teams\b"],
        ),
        DetectorSpec(
            app_key="zoom_web", display_name="Zoom", is_browser=True,
            url_patterns=[
                r"^https://[^/]+\.zoom\.us/wc/join/",
                r"^https://[^/]+\.zoom\.us/j/\d+",
            ],
            title_patterns=[r"(?i)\bZoom Meeting\b"],
        ),
        DetectorSpec(
            app_key="webex_web", display_name="Webex", is_browser=True,
            url_patterns=[r"^https://[^/]+\.webex\.com/(meet|wbxmjs|webappng)/"],
            title_patterns=[r"(?i)\bwebex\b"],
        ),
        DetectorSpec(
            app_key="whereby", display_name="Whereby", is_browser=True,
            url_patterns=[r"^https://whereby\.com/[^/]+"],
            title_patterns=[r"(?i)\bwhereby\b"],
        ),
        DetectorSpec(
            app_key="jitsi", display_name="Jitsi Meet", is_browser=True,
            url_patterns=[r"^https://meet\.jit\.si/[^/]+"],
            title_patterns=[r"(?i)\bjitsi\b"],
        ),
        DetectorSpec(
            app_key="8x8", display_name="8x8 Meet", is_browser=True,
            url_patterns=[r"^https://8x8\.vc/[^/]+"],
            title_patterns=[r"(?i)\b8x8\b"],
        ),

        # Web counterparts for the desktop messaging / meeting apps above.
        # Same display_name as the desktop spec so the consent-toast copy
        # doesn't surface the implementation detail of "is this the
        # desktop or web one"; users just see "Sayzo is ready to coach
        # you in Discord" either way. Whitelist watcher attributes the
        # match to whichever spec fired first — desktop apps run before
        # browser specs in match_whitelist's pass order, so a user with
        # both Discord desktop and discord.com open in a browser
        # attributes to the desktop app first.
        DetectorSpec(
            app_key="discord_web", display_name="Discord", is_browser=True,
            url_patterns=[r"^https://discord\.com/channels/"],
            title_patterns=[r"(?i)\bDiscord\b"],
        ),
        DetectorSpec(
            app_key="slack_web", display_name="Slack", is_browser=True,
            url_patterns=[r"^https://app\.slack\.com/client/"],
            title_patterns=[r"(?i)\bSlack\b"],
        ),
        DetectorSpec(
            app_key="skype_web", display_name="Skype", is_browser=True,
            url_patterns=[r"^https://web\.skype\.com/"],
            title_patterns=[r"(?i)\bSkype\b"],
        ),
        DetectorSpec(
            app_key="whatsapp_web", display_name="WhatsApp", is_browser=True,
            url_patterns=[r"^https://web\.whatsapp\.com/"],
            title_patterns=[r"(?i)\bWhatsApp\b"],
        ),
    ]


class ArmConfig(BaseSettings):
    """Configuration for the armed-only capture model.

    See ``~/.claude/plans/so-right-now-what-snug-corbato.md`` for the full
    design. The armed model replaces always-listening: audio streams only
    open after an explicit arm signal (hotkey or whitelist consent).
    """

    # Global hotkey binding. Users can change this via the Settings GUI (see
    # sayzo_agent.gui.settings) or the setup wizard. Values are written to
    # data_dir/user_settings.json and overlaid onto this default at load
    # time; SAYZO_ARM__HOTKEY env var still wins.
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
    # After the user declines or ignores the consent toast OR a
    # whitelist-armed session ends, we suppress new toasts for that
    # app_key until we observe the app release the mic continuously for
    # this long. "Not now" / "I just stopped" both mean "not this
    # meeting", so leaving + rejoining (a new session) fires a fresh
    # prompt, while staying in the same meeting stays quiet — the hotkey
    # is always available as an opt-in path during the suppressed window.
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
    # fires. Absorbs transient drops / immediate-rejoin scenarios — real
    # blips (mute/unmute re-enumeration, view/device swaps) are 1–3 s; 6 s
    # = three consecutive absent polls at the 2 s interval, enough to
    # filter them without making the post-meeting wait feel sluggish.
    whitelist_arm_release_grace_secs: float = 6.0
    meeting_ended_toast_timeout_secs: float = 15.0
    # After the user clicks "Keep going" on the meeting-ended toast we no
    # longer fire follow-up toasts (they already declined once), but if
    # the arm-app stays absent from mic-holders for this many consecutive
    # seconds we close the session and show an informational toast — the
    # original "Keep going" was issued under the assumption the meeting
    # was still live; a long sustained mic-release invalidates that
    # assumption. 90 s is well above plausible false-positive durations
    # (network reconnects in WebRTC apps top out at ~30 s, audio-device
    # switches under 5 s).
    force_close_after_keep_going_secs: float = 90.0
    # Deprecated as of v2.1.7: the meeting-ended watcher used to pause
    # for this many seconds after "Keep going" before re-checking. The
    # field is kept so existing user_settings.json files still parse
    # without errors; nothing reads it anymore.
    meeting_ended_snooze_secs: float = 600.0

    # Per-app detection rules. Populated from ``default_detector_specs()``.
    detectors: list[DetectorSpec] = Field(default_factory=default_detector_specs)

    # Sub-toggle for the "Sayzo is capturing — Press X to stop" toast fired
    # after every successful arm. Exposed in the Settings window. Master
    # ``Config.notifications_enabled`` still wins; consent + end-of-meeting
    # toasts are unaffected (they're how the user decides what to capture).
    notify_post_arm: bool = True

    # Sub-toggle for the long-meeting check-in consent ("Still in the
    # meeting?" at 1h / 2h / 2h30 / 3h / +30 min). When ``False``,
    # ``_run_checkins`` short-circuits and the agent never asks. Users
    # in deliberately long calls might prefer this off; the trade-off
    # is the session keeps capturing indefinitely until the user
    # disarms via hotkey or the joint-silence path fires.
    checkin_enabled: bool = True

    # Sub-toggle for the whitelist meeting-ended watcher ("Looks like
    # your meeting ended"). When ``False``, ``_run_meeting_ended_watcher``
    # short-circuits — the agent won't auto-suggest wrap-up when the
    # meeting app stops holding the mic. User must disarm manually.
    meeting_ended_watcher_enabled: bool = True

    # Sub-toggle for the hotkey-while-armed "Stop recording?" consent.
    # When ``False``, pressing the hotkey while armed disarms
    # immediately without asking — no safety net for accidental presses.
    confirm_hotkey_stop: bool = True

    # Sub-toggle for the "Wrapped up your session" info toast that
    # fires after the meeting-ended watcher's silent force-close (the
    # post-"Keep going" path that auto-closes once the arm-app has
    # been absent past the force-close threshold). Informational only;
    # the disarm still happens either way.
    notify_session_wrapped: bool = True


class AuthConfig(BaseSettings):
    auth_url: str = "https://sayzo.app/api/auth"
    client_id: str = "sayzo-desktop"  # Public OAuth client ID (no secret — PKCE)
    server_url: str = "https://sayzo.app"
    scopes: str = "offline_access upload"
    redirect_port: int = 17223  # Preferred port for PKCE localhost redirect
    login_timeout_secs: int = 120

    # When True the agent calls GET /api/me at service start + on a
    # background tick and refuses to arm if the server reports the
    # signed-in user hasn't finished onboarding at sayzo.app. Killable
    # via SAYZO_AUTH__ACCOUNT_CHECK_ENABLED=0 in case the endpoint is
    # unavailable or returning bad data and we need to roll back without
    # shipping a new agent.
    account_check_enabled: bool = True
    # Background refresh cadence for /api/me. Set to match the cache TTL so
    # the next refresh fires right as the cache would otherwise go stale —
    # no over-fetching, no stale window. The user-visible "I just finished
    # onboarding" path is handled by the FinishSignup screen's 8 s polling,
    # not this background tick. ~2.5 KB/req → ~10 KB/day/agent at 6h.
    account_refresh_interval_secs: float = 21600.0  # 6 hours

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


class NotificationConfig(BaseSettings):
    """Daily-drill notification scheduler config.

    Controls a per-workday notification that opens that day's pre-generated
    60-second speaking drill in the user's default browser. Timing is learned
    per-user via a (day_of_week, hour) acceptance-rate bucket model with
    Thompson-sampled hour selection.

    Master ``Config.notifications_enabled`` still wins — when False, the
    daily-drill scheduler doesn't even tick.
    """

    # Master sub-toggle. Default ON for signed-in users (the scheduler also
    # gates on TokenStore.has_tokens(), so an unauthenticated user is silent
    # regardless). Toggle in Settings → Notifications.
    daily_drill_enabled: bool = True

    # User must be idle (no kbd/mouse) at least this long before we fire,
    # to avoid interrupting an active meeting or a user mid-rush.
    min_idle_secs: float = 180.0

    # Cold-start hour (no engagement history yet). 11am local — outside
    # morning rush, before lunch, after standups.
    cold_start_hour: int = 11

    # Workday window (local time, 24-hour). max_hour exclusive.
    min_hour: int = 9
    lunch_start_hour: int = 12
    lunch_end_hour: int = 13     # exclusive — lunch covers 12 only
    max_hour: int = 17           # exclusive — last firing hour is 16

    # At/after this hour, if we never fired today, surface the EOD tray
    # fallback instead of firing late-evening "make-up" notifications.
    eod_fallback_hour: int = 17

    # Window for distinguishing tap (engaged) from soft_tap (late tap).
    # No-tap after dismiss_window_secs records as "expire". A late tap
    # within soft_tap_window_secs still counts as soft engagement.
    dismiss_window_secs: float = 300.0       # 5 min
    soft_tap_window_secs: float = 14400.0    # 4 h

    # Bucket scoring: smoothed_score =
    #   (taps*1.0 + soft_taps*0.3 + expires*0.0) * decay / (fires + alpha)
    # alpha=3 smooths a single bad fire so it can't permanently kill a slot.
    prior_alpha: float = 3.0

    # Per-bucket recency decay applied at read time:
    # decay = recency_decay ** days_since_bucket_last_fired_at.
    recency_decay: float = 0.95

    # Hours where smoothed_score < this AND fires >= 3 are excluded from
    # the candidate set. Single bad day shouldn't disqualify a slot.
    # Score range is [0, 1] in the dismiss-collapsed outcome model
    # (taps weight +1, soft_taps +0.3, expires 0); 0.05 ≈ 5% engagement
    # rate after smoothing. Thompson sampling does most exploration work
    # — this is a hard floor for "obviously dead" slots only.
    bad_score_threshold: float = 0.05

    # Maximum history-log entries kept on disk. Older entries roll into
    # bucket aggregates (no signal lost) but lose their per-event timeline.
    max_history: int = 200

    # Scheduler tick cadence. Each tick re-evaluates all gates; firing
    # happens once per workday max.
    tick_secs: float = 60.0


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
    echo_guard: EchoGuardConfig = Field(default_factory=EchoGuardConfig)
    arm: ArmConfig = Field(default_factory=ArmConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

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

    @property
    def notification_stats_path(self) -> Path:
        return self.data_dir / "notification-stats.json"

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
    for key in (
        "notifications_enabled",
        "notify_welcome",
        "notify_capture_saved",
    ):
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

    if isinstance(user.get("notifications"), dict):
        env_notif_keys = {
            k[len("SAYZO_NOTIFICATIONS__"):].lower()
            for k in os.environ
            if k.upper().startswith("SAYZO_NOTIFICATIONS__")
        }
        user_notif = {
            k: v for k, v in user["notifications"].items()
            if k.lower() not in env_notif_keys
        }
        if user_notif:
            init_kwargs["notifications"] = {
                **probe.notifications.model_dump(), **user_notif
            }

    if isinstance(user.get("capture"), dict):
        env_capture_keys = {
            k[len("SAYZO_CAPTURE__"):].lower()
            for k in os.environ
            if k.upper().startswith("SAYZO_CAPTURE__")
        }
        user_capture = {
            k: v for k, v in user["capture"].items()
            if k.lower() not in env_capture_keys
        }
        if user_capture:
            init_kwargs["capture"] = {
                **probe.capture.model_dump(), **user_capture
            }

    cfg = Config(**init_kwargs) if init_kwargs else probe
    cfg.ensure_dirs()
    return cfg
