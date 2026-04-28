# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Python agent that captures meetings on the user's machine — **only when the user says so**. Captures feed server-side analysis that drives personalized speaking drills in the Sayzo English-coaching webapp. Upload is a no-op stub (`NoopUploadClient`) until the user signs in.

The agent is in **armed-only mode** (v1.0+): audio streams are closed while disarmed, and only open after an explicit arm signal. Two arm paths:

1. **Hotkey** — global shortcut (default `Ctrl+Alt+S`, configurable in Settings). Pressing it shows a start-confirmation toast; on Yes / double-tap, the agent opens streams and captures until the user stops it or silence closes the session.
2. **Whitelist auto-suggest** — when the agent detects a meeting app (Zoom, Teams, Discord, Google Meet, etc.) is actually holding the microphone (not just running), it fires a consent toast: *"Sayzo is ready to coach you…"*. On Yes, same capture flow.

Everything runs locally (no paid APIs in the hot path). Armed sessions are bounded but can run for hours; the legacy 60-minute safety cap is removed in favor of the long-meeting check-in toast at 1h / 2h / 2h30 / 3h / every 30 min after.

## Install (Windows, Python 3.12)

The dependency graph has several traps. Follow this order exactly when setting up a fresh machine — `pip install -e .[dev]` alone will fail.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate

# 1. llama-cpp-python: prebuilt wheels live on a separate index, and pip will
#    silently fall back to source-build (which needs MSVC + CMake) unless you
#    force binary-only.
pip install llama-cpp-python --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

# 2. resemblyzer hard-pins source-only `webrtcvad`. Install its prebuilt
#    replacement first, then resemblyzer with --no-deps so pip doesn't
#    re-resolve the source one.
pip install webrtcvad-wheels librosa
pip install resemblyzer --no-deps

# 3. Now the rest.
pip install -e .[dev]
```

**New platform deps as of v1.0** (armed-only model):
- `pynput` + `psutil` (all platforms) — global hotkey + process queries.
- `pycaw` + `pywin32` (Windows only, marker-conditional) — WASAPI mic-session enumeration + foreground window.
- `pyobjc-framework-Cocoa` + `pyobjc-framework-CoreAudio` (macOS only, marker-conditional) — NSWorkspace frontmost-app + CoreAudio mic-active query.

These land via the normal `pip install -e .[dev]` step now — no special handling.

**Python 3.13 does not work** — `llama-cpp-python` has no prebuilt wheels for it. Stay on 3.12.

`resemblyzer` will print a `pip` warning that `webrtcvad` and `typing` are missing. Both are harmless: `webrtcvad-wheels` provides the same `webrtcvad` Python module under a different distribution name, and Python 3.12 has `typing` built in.

## Common commands

```bash
# Pure unit tests (no model loading, no audio I/O — fast)
pytest tests/

# Single test
pytest tests/test_conversation.py::test_gate_passes_late_substantive_user_turn -v

# CLI commands (all under one entrypoint)
sayzo-agent setup         # one-time: download Qwen GGUF (~2 GB) into ~/.sayzo/agent/models/
sayzo-agent devices       # list mic + loopback devices
sayzo-agent test-capture  # 10-second capture sanity check
sayzo-agent run           # main 24/7 loop with verbose terminal output
```

Captures land in `./sayzo-data/captures/<id>/` (project-local by default, override with `SAYZO_DATA_DIR`) as `record.json` + `audio.opus`.

### Persistence vs. upload (don't get these confused)

`CaptureSink.write` in `sink.py` persists a kept session to disk. It runs **before** `UploadClient.upload`. Two upload clients exist:

- `AuthenticatedUploadClient` (`upload.py`) — real multipart POST to `/api/captures/upload`. Active when the user is logged in and `cfg.auth.effective_server_url` is set. Failures are caught and logged; they do not raise back to the pipeline and they do not delete the local files.
- `NoopUploadClient` — fallback when the user is unauthenticated. Only logs `"[upload] (noop) …"`.

Either way, every session that survives the pipeline is saved locally first and survives restarts. The sink logs absolute paths of `record.json` + `audio.opus` so you can see where they landed. Do not delete local files on upload success — the sink write is the source of truth.

### Desktop notifications

`sayzo_agent/notify.py` owns a dedicated asyncio loop on a daemon thread (constructed eagerly in `__init__`) so interactive consent toasts can marshal button callbacks back to us via `desktop-notifier`'s async API. Two public methods:

- `notify(title, body)` — fire-and-forget toast (capture saved, post-arm guidance, stream-open error, welcome).
- `ask_consent(title, body, yes_label, no_label, timeout_secs, default_on_timeout) -> "yes" | "no" | "timeout"` — interactive toast with two action buttons, used by the ArmController for all consent flows (whitelist, hotkey start/stop confirmation, end-of-meeting confirmation, check-in, meeting-ended).

Toggle all non-consent toasts with `SAYZO_NOTIFICATIONS_ENABLED=0`. Consent + end-of-meeting toasts are always on (they're how the user decides what gets captured). AUMID (`"Sayzo"`, set in `installer/windows/sayzo-agent.nsi` and passed to `DesktopNotifier(app_name="Sayzo")`) is required on Windows 10 for WinRT toasts + buttons to render. macOS needs a signed bundle with `CFBundleIdentifier = com.sayzo.agent` (already set in `sayzo-agent.spec`) for NSUserNotification action buttons.

### Heartbeat log

`[heartbeat]` line every `Config.heartbeat_secs` seconds (default 30, `SAYZO_HEARTBEAT_SECS=0` disables). Shows arm state (`ARMED` + reason tag like `(zoom)` / `(hotkey)`, or `DISARMED`), detector state (`OPEN` / `PENDING_CLOSE` / `IDLE`), elapsed / pre-buffer / silence counters, LLM loaded/unloaded, running kept/discarded counters. Lets a user watching the terminal for hours tell at a glance whether the agent is alive, what it's currently doing, and why it's currently armed.

## Architecture

The pipeline is **staged by cost** — cheap stages run continuously (while armed), expensive stages only run on data that survived the cheap gates. The arm model is layered on top: when disarmed, zero audio flows. When armed, the same pipeline the agent always had runs.

```
ArmController (DISARMED on launch)
    ↕ [hotkey press → start-confirm toast → arm]
    ↕ [whitelist match → consent toast → arm]
ArmController.arm() → vad.reset() + detector.reset_source_epochs()
                    + detector.open_session_on_arm(now)  ← session opens at arm time, not at first VAD
                    + mic.start() + sys.start()
    ↓
mic + system capture (asyncio queues, 16 kHz mono)
    ↓ [only flows while armed_event is set; mic.queue is drained on stop+start]
Silero VAD (per source, stateful)
    ↓ [emits SpeechSegment events]
ConversationDetector (silence-bounded sessions)
    ↓ [appends PCM + segments to the already-open session buffer]
[joint silence 45s → PENDING_CLOSE → end-confirmation toast]
[toast Yes/timeout → commit_close → sink path; toast No/speech → revert]
    ↓
Cheap pre-STT gate (substantive user turn rule)
    ↓ [whole session passes or whole session is dropped]
faster-whisper (transcribe mic + system separately)
    ↓
Speaker tagging (mic = "user" by definition; Resemblyzer greedy clustering on system audio for "other_1", "other_2", …)
    ↓
Relevance LLM (Qwen 2.5 3B Q4 via llama-cpp-python)
    ↓ [judges participant + extracts relevant span + title + summary]
Post-capture DSP (highpass + spectral-gate denoise on mic; light HPF on system)
    ↓ [cleans the on-disk audio without affecting STT, which already ran]
CaptureSink (Opus stereo: mic=L, system=R; record.json)
    ↓
UploadClient (NoopUploadClient until user signs in)
```

### Arm model (sayzo_agent/arm/)

`ArmController` in `arm/controller.py` is the single source of truth for armed state. Its background tasks (launched from `arm()`, cancelled on `disarm()`):

- **Whitelist watcher** (runs while DISARMED) — polls every `ArmConfig.poll_interval_secs` (default 2 s). Uses `platform_win.get_mic_holders()` / `platform_mac.is_mic_active()` + `get_foreground_info()` to build a `MicState` + `ForegroundInfo`. Feeds those to `detectors.match_whitelist()`. On match, fires the consent toast via `notifier.ask_consent()`. Per-app cooldown (30 min after decline, 10 min after session) keyed by `app_key`.
- **Long-meeting check-in task** (runs while ARMED) — sleeps until each `long_meeting_checkin_marks_secs` mark from session-start, fires "Still in the meeting?" toast. "Wrap up" → disarm with reason `CHECKIN_WRAP_UP`.
- **Meeting-ended watcher** (runs while whitelist-armed; NOT for hotkey-armed sessions) — polls mic-holders; if the arm-app hasn't held the mic for `whitelist_arm_release_grace_secs` (default 6 s = three absent polls at the 2 s interval), fires "Looks like your meeting ended" toast. "Keep going" snoozes `meeting_ended_snooze_secs` (default 10 min), then re-fires if still absent. Non-response defaults to Wrap up.

**Detection is mic-holder-based, not window-title-based.** `detectors.match_whitelist()` is pure logic operating on `MicState.holders` (Windows: pycaw WASAPI session enumeration) or `MicState.active + running_processes + foreground` (macOS: `kAudioDevicePropertyDeviceIsRunningSomewhere` + psutil + NSWorkspace). Works for Discord (which never changes window title during calls), survives app updates, mute-tolerant (muted users still have an active capture session).

Default whitelist ships with 21 apps (14 desktop + 7 web) — see `config.py::default_detector_specs()`. Users edit the list via Settings → Meeting Apps (see `gui/webui/src/settings/MeetingAppsPane.tsx` + `AddAppDialog.tsx`, backed by `gui/settings/bridge.py`): toggle off / remove / one-click-add from a live mic-holder picker (desktop) or a pasted meeting URL (web). The Suggested-to-add section is driven by `arm/seen_apps.py`, which records any unmatched mic-holder the watcher observes while disarmed (capped at 20 entries). The in-app edit writes the full list to `user_settings.json` under `arm.detectors` and nudges the live agent over IPC to reload; `SAYZO_ARM__DETECTORS` env var still wins.

### Session state machine

`ConversationDetector` has three states:

- **IDLE** → no session. The ArmController calls `open_session_on_arm(now)` on every arm to transition into OPEN. Frames received while IDLE are **dropped on the floor** — there is no pre-buffer in armed-only mode (v2.1.7+); IDLE means "nothing should be coming through" and any frame that does is either a stale leftover from a previous arm cycle (e.g. `mic.queue` not fully drained) or post-close bleed-through, and either way it must not pollute the next session. The legacy `_open_session(now, trigger, vad_ts)` VAD-trigger path still exists as a fallback for unit tests that feed segments without frames.
- **OPEN** → session in progress. Joint silence ≥ `joint_silence_close_secs` transitions to…
- **PENDING_CLOSE** → buffers still held, nothing written to disk. `on_pending_close` callback (the ArmController) shows the end-confirmation toast:
  - `commit_close(reason)` — finalize: push buffers to `_closed_queue`, go back to IDLE, sink picks it up via `_ticker`.
  - `revert_close(now)` — cancel close: back to OPEN, silence timer reset.
  - VAD segment during PENDING_CLOSE → auto-revert (user resumed speaking is ground truth).
  - Legacy unit-test path (no callback registered) → commit immediately, preserving pre-armed-model behavior.

**Gap-fill cap (v2.1.7+).** `on_frame` zero-fills small dropped-frame gaps to preserve the sample-to-mono-time invariant, but caps any single fill at `ConversationConfig.max_gap_fill_secs` (default 2 s). A larger gap is never a real audio dropout — it's stale state (stale frame from before the current arm cycle, system suspend / resume, USB reconnect) — and the detector re-anchors instead of injecting silence.

New `SessionCloseReason` values: `HOTKEY_END`, `CHECKIN_WRAP_UP`, `WHITELIST_ENDED`. `SAFETY_CAP` is kept in the enum for backward compat with on-disk records but nothing in the new code path emits it (the 60-min cap is removed).

### Critical design rules (do not regress these)

These are the rules the conversation detector and gate logic encode. Several were added in response to specific failure modes the user identified during planning — re-introducing any of them silently breaks the product's core value.

1. **Sessions are bounded by sustained silence, not by fixed time windows.** The detector must NOT require mic and system speech to co-occur inside a rolling window. A 10-minute user demo with one "thanks" at the end is a single session and must be captured whole. Same for the inverse (long other-side monologue + short user reply).

2. **The substantive-user-turn rule lives in `evaluate_user_turn_gate`** in `conversation.py`. A session passes only if the user has either (a) one continuous turn ≥ `min_user_turn_secs` (default 8 s), OR (b) cumulative voiced time ≥ `min_user_total_secs` over ≥ `min_user_turns_for_total` distinct turns. **The user's substantive turn may come anywhere in the session — including at the very end.** Discarding a session early because the first user turn was an "mhm" is a bug.

3. **When a session passes the gate, the *whole* session is transcribed**, not a trimmed slice. The other-side speech surrounding the user's turns is required context for downstream analysis.

4. **The relevance LLM must NOT crop tightly.** Its prompt instructs it to be generous. As a safety net, `RelevanceLLM._parse` also pads `relevant_span` by ±15 s (`SPAN_PAD_SECS`) before returning. Small local LLMs crop too tight even when told not to — the padding is intentional.

5. **Whisper hallucinates "Thank you" / "Thanks for watching" on silence.** Mitigated in `WhisperSTT.transcribe_pcm16` by `vad_filter=True`, `condition_on_previous_text=False`, and tightened `no_speech_threshold` / `log_prob_threshold`. Don't loosen these.

### Module responsibilities

- **`conversation.py`** — Pure (no I/O, no models). State machine (IDLE / OPEN / PENDING_CLOSE) + gate. Unit-tested with synthetic VAD events. The most behavior-critical file in the project; treat changes here with care.
- **`app.py`** — Async orchestrator. Wires ArmController → capture → VAD → detector → heavy-worker pool → sink. `_consume` waits on `arm.armed_event` so frames only flow while armed. All STT/embedding/LLM work runs on a single-worker `ThreadPoolExecutor` so heavy stages never run in parallel and starve the CPU. Capture + VAD run on the asyncio loop.
- **`arm/controller.py`** — ArmController state machine. Owns stream lifecycle, hotkey confirmations, whitelist consent, PENDING_CLOSE handling, long-meeting check-ins, meeting-ended watcher. Unit-tested with fake capture / VAD / notifier / injected platform queries.
- **`arm/detectors.py`** — Pure matching logic against `DetectorSpec` list from config. No OS calls. Skips specs with `disabled=True` (the Meeting Apps pane's off toggle).
- **`arm/seen_apps.py`** — Persistence for unmatched mic-holders observed by the whitelist watcher. Written to `data_dir/seen_apps.json`. Capped at 20 entries, dedup'd by lower-cased key, scrubbed against the current whitelist on read. Drives the Settings → Meeting Apps "Suggested to add" section.
- **`arm/platform_win.py` / `arm/platform_mac.py`** — Platform-specific OS queries: Windows uses pycaw for mic-session enumeration + `win32gui` for foreground; macOS uses pyobjc CoreAudio for mic-active bit + NSWorkspace for frontmost + AppleScript (cached 2 s) for active tab URL.
- **`arm/hotkey.py`** — pynput global hotkey listener. Marshals key-press events from pynput's listener thread onto the arm controller's asyncio loop via `call_soon_threadsafe`. Requires Accessibility permission on macOS (graceful fallback: tray menu still works).
- **`notify.py`** — Desktop notifier with a dedicated background asyncio loop (eager `__init__`) so interactive consent toasts can await button callbacks. `NoopNotifier.ask_consent` always returns the default, for unit-test clean paths.
- **`settings_store.py`** — JSON load/save for `data_dir/user_settings.json`. The Settings GUI + onboarding write here; `load_config` overlays onto `ArmConfig` defaults (env vars still win over JSON).
- **`relevance.py`** — Loads Qwen lazily on first use, unloads after `idle_unload_secs` (default 5 min) to free ~2 GB of RAM during idle periods. The system prompt is the contract — modifying it changes the JSON shape downstream.
- **`capture/system.py`** — Uses PyAudioWPatch for WASAPI loopback capture. Captures at the device's native sample rate (typically 48 kHz) and resamples to 16 kHz via scipy to avoid quality loss.
- **`speaker.py`** — Greedy cosine clustering for other-speaker labels (avoids a sklearn dependency). Heavy imports (`resemblyzer`) are lazy so unit tests don't need them.
- **`dsp.py`** — Pure numpy/scipy post-processing applied at session close, before Opus encoding. Butterworth highpass + `noisereduce` spectral-gate denoise (default `prop_decrease=0.5`, dialed down from 0.85 to avoid phasey artifacts) + peak-normalize on mic; light highpass + peak-normalize on system (no denoise — system audio is typically a clean digital stream already, and aggressive denoising damages music / low-volume speech from the far side). Runs on the heavy-worker executor and is fully decoupled from STT: transcription and speaker embedding read the raw `buffers.mic_pcm` upstream, so DSP here has zero impact on whisper accuracy. All stages are config-flagged under `CaptureConfig` — `SAYZO_CAPTURE__DSP_ENABLED=0` restores raw-PCM output byte-for-byte (minus the encoder's `application` setting, which is intrinsic to the sink path). Opus encoder knobs also live in `CaptureConfig` (`opus_bitrate`, `opus_application`). Default is `application="audio"` at 96 kbps stereo — transparent for speech, usable for music. Capture modules no longer apply per-batch RMS normalization (previously `_TARGET_RMS=0.02` caused audible pumping); raw captured levels flow through and DSP's peak-normalize at session close handles final loudness.

## Distribution caveats (future work)

The current install is fragile by design — it's a dev install, not a distributable. If/when shipping to non-dev users:

- `resemblyzer` forces source-build of `webrtcvad` → replace with a directly-loaded ONNX speaker encoder.
- `llama-cpp-python` wheels are Python-version-fragile → bundle via PyInstaller/Nuitka instead of relying on pip.
- Models are downloaded post-install via `huggingface_hub` (cached, idempotent) — this part is fine to keep.

The plan in `~/.claude/plans/linear-squishing-bird.md` (referenced from session history) has the full rationale.
