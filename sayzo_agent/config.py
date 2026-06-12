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
    #   the user only notices when their coaching comes back empty. The
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
    # Peak-normalize target. Lowered v3.6.4 from -1 dBFS to -3 dBFS after a
    # user dogfood report: when AEC cancels strongly (mic RMS drops 3+ dB),
    # peak-normalize compensates by applying extra gain to reach the target —
    # which AMPLIFIES constant background (fan hum, room tone, electrical
    # noise) along with the legitimate signal. Lower target = less baseline
    # gain = less background lifted.
    peak_normalize_dbfs: float = -3.0
    # Hard ceiling on the gain peak-normalize is allowed to apply, in dB.
    # 6 dB = 2x amplification max. Prevents pathological lift on quiet
    # post-AEC captures (where the target/peak ratio could otherwise demand
    # 15-20 dB of gain and turn background hum into audible static). Output
    # peak may end up below ``peak_normalize_dbfs`` for very quiet inputs;
    # that's the intended trade-off (quieter playback over amplified hum).
    # Set 0 to disable amplification entirely; set a high value (e.g. 60)
    # to restore pre-v3.6.4 unbounded behavior.
    peak_normalize_max_gain_db: float = 6.0

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
    # Single-threshold substantive-user-turn rule (v3.5.2+): a session passes
    # iff the user's cumulative voiced time is at least this many seconds,
    # however distributed — one long turn, many short turns, doesn't matter.
    # The pre-v3.5.2 dual-path rule (8s single-turn OR 10s cumulative across
    # ≥2 turns) used an AND inside the cumulative branch that surprised
    # users into thinking a session with several 2-3s turns adding to 8s+
    # should pass when it didn't. echo_guard handles "user accidentally let
    # a podcast play into their mic" cases on a separate pass, so a single
    # long VAD segment can't game this threshold.
    min_user_total_secs: float = 8.0
    min_sys_voiced_secs: float = 1.0
    # Pad around each VAD segment (mic or system) when building the final
    # saved audio. Regions outside any padded segment are zero-filled so
    # dead air + static artifacts don't end up in the on-disk capture. Small
    # pad keeps speech starts/ends from being clipped.
    final_audio_speech_pad_secs: float = 0.5
    # DEPRECATED v3.7.0 — unused. The pre-v3.7 trim pipeline used this to
    # decide which mid-conversation VAD gaps to zero-fill on disk; v3.7.0
    # replaced that with `apply_session_trim` (sayzo_agent/session_trim.py),
    # which slices to [first_speech-pad, last_speech+pad] and preserves all
    # mid-region audio. Kept here so existing user_settings.json files don't
    # fail pydantic validation if the per-class `extra` policy isn't 'ignore'.
    # Remove in v3.8.
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
    # Hard upper bound on a single zero-fill gap. Below the cap, gaps are
    # zero-filled to preserve the sample-to-mono-time invariant required for
    # mic↔sys alignment in the AEC pre-pass. Above it, the detector
    # re-anchors as a safety valve so a literal system-suspend (minutes long)
    # doesn't inject minutes of silence into the session.
    #
    # 30 s tolerates realistic sys-capture startup delays: WASAPI pa.open +
    # silence-pump open + first 500 ms batch read takes 1–5 s typical and up
    # to ~10 s on cold-cache COM init. The pre-v3.6 default of 2 s was too
    # tight, so cold starts hit the re-anchor branch and misaligned mic vs
    # sys by the startup delay — silently breaking AEC for every session.
    # Stale frames (capture_mono_ts < session_t0_mono) are now detected
    # separately in `on_frame` and dropped explicitly, so this cap no longer
    # has to do double duty as the stale-frame guard.
    max_gap_fill_secs: float = 30.0


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
    #
    # v3.6.6: tightened from 0.50 → 0.30 to match the D-recipe prototype
    # (scripts/synth_double_talk_test.py). With triple-AEC running upstream,
    # the linear filter has already taken three swings at the bleed; what
    # survives onto the mic in the speech band tends to be the harder cases
    # (cheap-speaker compression artifacts, BT codec re-encoding) where the
    # remaining coherence is lower but still attributable to sys content. A
    # 0.30 threshold catches those without dropping legitimate user turns,
    # since residual-speech-prob (the AND-partner gate) keeps real speech.
    coh_high_threshold: float = 0.30  # mean weighted speech-band coherence
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
    #
    # v3.6.6: tightened the whole subdivide tuple (4.0/1.0/0.25 → 0.3/0.3/0.1)
    # to match the D-recipe prototype. The 4.0 s minimum was too coarse — a
    # 3 s "you. okay so back to" merged segment (user "okay so back to" +
    # other-side "you." bleeding through) would never get subdivided and
    # would ride through as one whole-segment classification. With the 0.3 s
    # minimum + 0.3 s window + 0.1 s hop, the same segment splits into ten
    # 0.3 s sub-windows each scored independently; only the actual bleed
    # span gets zeroed.
    subdivide_long_segments_secs: float = 0.3  # 0 disables
    subdivide_window_secs: float = 0.3
    subdivide_hop_secs: float = 0.1

    # Cosine fade at zero'd-region boundaries so the encoder doesn't see a
    # spectral cliff at echo → user transitions on the mic channel.
    taper_ms: float = 5.0

    # Opt-in: dump per-dropped-segment WAVs (mic + sys + residual) under
    # <data_dir>/logs/echo_debug/<session_id>/ for offline inspection.
    debug: bool = False


class AecConfig(BaseSettings):
    """WebRTC AEC3 pre-pass over (mic_pcm, sys_pcm) before echo_guard.

    The current echo_guard is a per-segment classifier — it can drop a
    whole VAD segment if it looks like echo, but it can't subtract a
    speaker-bleed *signal* from a mic segment that also contains real
    user speech (double-talk). This AEC pass uses WebRTC's
    AudioProcessingModule (AEC3) to predict the echo from the system
    audio reference and subtract it from the mic at the sample level.

    Runs in `app._process_session_inner` AFTER session close, BEFORE
    `echo_guard.classify_buffers`, on the existing heavy-worker
    ThreadPoolExecutor. echo_guard then sees an already-cleaned mic
    and becomes the non-linear residual safety net (e.g. cheap-laptop
    speaker driver compression, BT codec re-encoding artifacts).

    v3.4.0 shipped with ``enabled=False`` (opt-in via env var or
    Settings toggle) so we could dogfood the AEC integration without
    risking regressions on the existing recording path. v3.6.1 flips
    the default ON after the v3.6.0 mic↔sys alignment fix made AEC
    actually effective in production: per-segment cancellation up to
    25 dB when the user is silent, 4–10 dB during double-talk, and
    audibly clearer recordings on every speaker setup the team
    dogfooded.
    """

    # Master switch. SAYZO_AEC__ENABLED=0 to turn off.
    enabled: bool = True

    # Reference-stream delay alignment.
    # Mic and sys arrive on independent device clocks (sounddevice mic
    # vs WASAPI loopback on Win / Process Tap helper on Mac); a global
    # lag of hundreds of ms is normal — WASAPI loopback buffering alone
    # can be 100-200 ms, plus sounddevice mic callback latency
    # (~30-100 ms). 500 ms gives the xcorr enough headroom to find the
    # real peak in most setups; 200 ms (v3.5.0 default) was clipping
    # real-world lags at the search boundary on speaker-equipped
    # laptops. Echo_guard's own xcorr (see echo_guard.estimate_delay)
    # is what we reuse here.
    lag_search_ms: int = 500
    # Cap the lag we pass to set_stream_delay_ms; xcorr lags larger
    # than this on a session-wide estimate usually indicate spurious
    # correlation (silence + silence aligns at random offsets), so
    # we fall back to 0 and let AEC3's internal delay tracker handle it.
    # Matched to lag_search_ms so the entire search range is trusted.
    lag_max_ms: int = 500
    # Minimum xcorr peak (normalized) below which we don't trust the
    # estimated lag and fall back to 0. echo_guard uses 0.15; the AEC
    # global pass operates on much longer windows so we expect higher
    # peaks when there's any real echo path.
    min_xcorr_peak: float = 0.10

    # Skip thresholds (cheap exits — no echo possible, don't waste CPU).
    # If either channel is essentially silent, AEC is a no-op; bail.
    min_mic_rms: float = 0.0005   # ~-66 dBFS in float32
    min_sys_rms: float = 0.0005

    # Additional WebRTC AudioProcessingModule features beyond AEC3.
    #
    # NS3 (noise suppression) is exposed but OFF by default. It uses
    # spectral subtraction tuned for VoIP human-listening and produces
    # audible "musical noise" / comfort-noise artifacts on the
    # already-clean signal coming out of AEC3 — a real user heard a
    # static crackle on a headphones-only capture once NS3 was enabled
    # in v3.5.2 dogfooding (env-var override capture confirmed NS3 was
    # the culprit). Industry consensus for ASR/agent pipelines is to
    # pick ONE noise suppressor; we already ship `noisereduce` in
    # dsp.py tuned at prop_decrease=0.5 against this exact artifact
    # shape. Keep the field so it can be turned back on via env var or
    # Settings for experimentation, but the default is off. The
    # `denoise_enabled = False` skip in app._process_session_inner is
    # the guard rail for when someone explicitly flips this on — keeps
    # NS3 + noisereduce from stacking and compounding the artifact.
    #
    # HPF (high-pass filter) stays ON by default — it's a fixed-cutoff
    # high-pass that kills DC and sub-80Hz rumble; no known artifacts
    # on clean input. Free win, no trade-off.
    #
    # AGC stays hardcoded OFF in aec.py — it pumps mic gain during
    # far-side monologue and boosts ambient noise to speech level,
    # confusing Deepgram's diarization. Our peak-normalize in dsp.py
    # is the one-shot loudness pass that handles overall level without
    # AGC's per-frame mischief.
    noise_suppression: bool = False
    high_pass_filter: bool = True


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

    # Whitelist consent toast timings. 60s (was 30s): a borderless-
    # fullscreen meeting window (Chrome/Meet, Zoom) can occlude the toast
    # for a while — see the HWND_TOPMOST re-assert in gui/hud/window.py
    # (_force_topmost_win), the actual fix for "couldn't see the toast".
    # A longer window gives the user more chance to notice it once it
    # surfaces. NOTE: a timeout is STILL treated as a decline (suppresses
    # re-prompts for the rest of the meeting via _Cooldowns.mark_declined);
    # lengthening the window is the deliberate no-nag tradeoff chosen over
    # re-prompting.
    consent_toast_timeout_secs: float = 60.0

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


class HudConfig(BaseSettings):
    """Visual settings for the on-screen HUD (the floating capture pill,
    consent cards, toasts).

    Top-level rather than nested under ``ArmConfig`` because visibility is
    about the UI surface, not the arming logic — keeps room for additional
    HUD prefs (animation intensity, position, etc.) without re-organising.
    """

    # When False, the floating "recording indicator" (StatePill) doesn't
    # appear while a session is armed — the user still arms / disarms via
    # hotkey or tray menu, and consent cards / toasts still fire. Chosen
    # during first-run onboarding and changeable via Settings → Recording.
    # Default True preserves current behaviour for upgrading users and
    # gives new users the live trust signal during their first arm.
    show_recording_indicator: bool = True

    # Parent→HUD liveness ping cadence (seconds). The launcher pings the
    # HUD subprocess this often; if it stops answering (Qt loop deadlock,
    # GPU hang) the subprocess is killed and respawned. 0 disables —
    # same convention as Config.heartbeat_secs. Override via
    # SAYZO_HUD__HEARTBEAT_SECS.
    heartbeat_secs: float = 30.0


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
    # Post-capture coaching insight card (v3.10+). When True, once the server
    # finishes analyzing a capture the agent shows ONE specific, grounded
    # coaching insight as a compact HUD card (see capture_poller.py). Default
    # ON — it's the engagement payoff. The card carries a one-click "Stop
    # showing these" button that flips this to False; also toggleable in
    # Settings → Notifications. When ON it REPLACES the immediate "Conversation
    # saved" toast (the insight card deep-links too); the saved toast only
    # fires as a fallback when no insight is produced. SAYZO_NOTIFY_CAPTURE_FEEDBACK=0
    # to disable. Master ``notifications_enabled`` still wins.
    notify_capture_feedback: bool = True
    # Remote diagnostics (v3.16+). When True the agent (a) piggybacks app
    # version + OS + a per-install id onto the existing /api/me poll so we can
    # see who's on Mac/Windows and what version, and (b) may upload its
    # PII-free ``agent.log`` on demand (server-flagged) or after a crash. This
    # single flag gates ALL diagnostics surfaces (see diagnostics.py) — when
    # False, no inventory headers, no on-demand pull, no crash upload, and no
    # install-id file is ever written. Opt-out: default ON, disclosed in the
    # onboarding Done screen + Settings. SAYZO_SHARE_DIAGNOSTICS=0 to disable.
    share_diagnostics: bool = True

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    echo_guard: EchoGuardConfig = Field(default_factory=EchoGuardConfig)
    aec: AecConfig = Field(default_factory=AecConfig)
    arm: ArmConfig = Field(default_factory=ArmConfig)
    hud: HudConfig = Field(default_factory=HudConfig)
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
    for key in (
        "notifications_enabled",
        "notify_welcome",
        "notify_capture_saved",
        "notify_capture_feedback",
        "share_diagnostics",
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

    if isinstance(user.get("aec"), dict):
        env_aec_keys = {
            k[len("SAYZO_AEC__"):].lower()
            for k in os.environ
            if k.upper().startswith("SAYZO_AEC__")
        }
        user_aec = {
            k: v for k, v in user["aec"].items()
            if k.lower() not in env_aec_keys
        }
        if user_aec:
            init_kwargs["aec"] = {
                **probe.aec.model_dump(), **user_aec
            }

    if isinstance(user.get("hud"), dict):
        env_hud_keys = {
            k[len("SAYZO_HUD__"):].lower()
            for k in os.environ
            if k.upper().startswith("SAYZO_HUD__")
        }
        user_hud = {
            k: v for k, v in user["hud"].items()
            if k.lower() not in env_hud_keys
        }
        if user_hud:
            init_kwargs["hud"] = {
                **probe.hud.model_dump(), **user_hud
            }

    cfg = Config(**init_kwargs) if init_kwargs else probe
    cfg.ensure_dirs()
    return cfg
