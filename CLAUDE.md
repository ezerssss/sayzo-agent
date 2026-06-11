# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Python agent that captures meetings on the user's machine — **only when the user says so**. Captures feed server-side analysis that powers personalized coaching on each conversation in the Sayzo English-coaching webapp, where the user can replay a conversation to practice it. Upload is a no-op stub (`NoopUploadClient`) until the user signs in.

The agent is in **armed-only mode** (v1.0+): audio streams are closed while disarmed, and only open after an explicit arm signal. Two arm paths:

1. **Hotkey** — global shortcut (default `Ctrl+Alt+S`, configurable in Settings). Pressing it shows a start-confirmation toast; on Yes / double-tap, the agent opens streams and captures until the user stops it or silence closes the session.
2. **Whitelist auto-suggest** — when the agent detects a meeting app (Zoom, Teams, Discord, Google Meet, etc.) is actually holding the microphone (not just running), it fires a consent toast: *"Sayzo is ready to coach you…"*. On Yes, same capture flow.

Everything runs locally (no paid APIs in the hot path). Armed sessions are bounded but can run for hours; the legacy 60-minute safety cap is removed in favor of the long-meeting check-in toast at 1h / 2h / 2h30 / 3h / every 30 min after.

## Install (Windows, Python 3.12)

One step on a fresh machine:

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

No special preamble — `faster-whisper` / `resemblyzer` / `webrtcvad-wheels` / `librosa` were dropped in v3.0 when on-device transcription + speaker embedding moved to the server (Deepgram Nova-3 multichannel + diarize). Contributors no longer need a C toolchain to set up the venv.

**Platform deps**:
- `pynput` + `psutil` (all platforms) — global hotkey + process queries.
- `pycaw` + `pywin32` (Windows only, marker-conditional) — WASAPI mic-session enumeration + foreground window.
- `pyobjc-framework-Cocoa` + `pyobjc-framework-ApplicationServices` (macOS only, marker-conditional) — NSWorkspace frontmost-app + AX browser window-title / URL reads. (CoreAudio bindings are no longer used from Python — the `audio-detect` Swift helper owns that surface; pyobjc-framework-CoreAudio dependency was dropped in v2.5.)
- `PySide6` + `PySide6-Addons` — HUD subprocess (QtWebEngine for per-pixel-alpha transparency on Windows 10+).

## Common commands

```bash
# Pure unit tests (no model loading, no audio I/O — fast)
pytest tests/

# Single test
pytest tests/test_conversation.py::test_gate_passes_late_substantive_user_turn -v

# CLI commands (all under one entrypoint)
sayzo-agent first-run     # one-time: log in + start the service
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

### Notifications: custom HUD overlay (v2.10+)

Sayzo no longer uses OS notification APIs. Every user-facing toast — capture pill, consent prompts, info toasts, post-capture coaching cards — renders inside a frameless, transparent, always-on-top **PySide6 + QtWebEngine** window that the agent owns end-to-end (the original v2.10 build used pywebview; it was rewritten on Qt in v2.11 for genuine per-pixel alpha). The legacy `desktop-notifier` / `UNUserNotificationCenter` / `NSUserNotification` / `osascript display dialog` paths were removed in v2.10 after years of "no toast appeared" incidents driven by AUMID drift, Focus-mode banner dropping, unsigned-bundle silent denial, and stale TCC entries across signing changes.

**v3.14 robustness pass** hardened this subsystem (see `[[project_hud_hardening_v3_14_0]]`): a parent→HUD heartbeat (`SAYZO_HUD__HEARTBEAT_SECS`, default 30, 0 disables) that kills + respawns a hung-but-alive subprocess; QtWebEngine `renderProcessTerminated` recovery (reload once, then child `os._exit(3)` so the parent respawns); a 60 s child ready-watchdog (`os._exit(4)` if React never hands-shakes); JS dispatch via `JSON.parse(json.dumps(...))` instead of a template literal (kills `${...}` injection from server/transcript text); a single unified respawn path; recording-pill replay after any respawn/renderer-reload; a Windows `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` topmost re-assert; and a tray "Notifications unavailable" line when the ladder gives up (cleared on the next arm via `HudLauncher.reset_given_up`). Child exit codes **3** (renderer double-death) and **4** (ready watchdog) both mean "parent, please respawn me."

The HUD architecture has four pieces:

- **`sayzo_agent/notify.py`** — public Notifier Protocol (`notify`, `ask_consent`, `notify_actionable`, `notify_insight`, `has_authorisation_sync`; `notify_insight` added in v3.10 for the post-capture coaching card). Two implementations:
  - `HudNotifier(launcher)` — wraps a `HudLauncher`; thin adapter that forwards every method.
  - `NoopNotifier` — silent fallback for unit tests and `SAYZO_NOTIFICATIONS_ENABLED=0`.
- **`sayzo_agent/gui/hud/launcher.py::HudLauncher`** — parent-process subprocess manager. Spawns `sayzo-agent hud --idle` at agent boot, writes newline-delimited JSON commands over stdin (`show_pill`, `show_card`, `show_toast`, `show_actionable`, `hide_pill`, `quit`), reads response events over stdout (`hud_ready`, `card_response`, `actionable_response`, `pill_stop_clicked`, …). Resolves a per-call `concurrent.futures.Future` so `ask_consent` keeps its synchronous-blocking contract from the legacy `DesktopNotifier`. Bounded respawn ladder (5 s / 15 s / 60 s, then give-up) for crash recovery.
- **`sayzo_agent/gui/hud/window.py::HudWindow`** — runs in the subprocess. A frameless `QWidget` host (`WA_TranslucentBackground`) wrapping a `QWebEngineView`; per-pixel alpha gives "invisible when empty" without show/hide juggling. Top-right of the cursor's screen, realized at full opacity on `loadFinished`. The React app calls the JS-bridge `set_window_visible(bool)` whenever its content goes empty / non-empty; the show path re-anchors, runs a 1-px `setGeometry` paint-refresh (Windows layered-window), and re-asserts overlay z-order — macOS `orderFrontRegardless` (LSUIElement-parent case), Windows `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)` (also re-fired by the foreground WinEvent hook). Overlay tweaks: macOS `NSStatusWindowLevel` + collection behavior `CanJoinAllSpaces | FullScreenAuxiliary | Transient | IgnoresCycle` + `hidesOnDeactivate=False`; Windows `Qt.Tool | WindowStaysOnTopHint` (→ `WS_EX_TOOLWINDOW | WS_EX_TOPMOST`) plus a `WS_EX_TRANSPARENT` click-through toggle (NOT `WS_EX_NOACTIVATE` — that flag blocks the embedded web content from routing mouse clicks). The stdin reader (daemon thread) marshals commands onto the Qt GUI thread via a `Signal`; dispatch into React is `runJavaScript(build_dispatch_js(raw))` (see `gui/hud/js_escape.py`).
- **React HUD app** at `gui/webui/src/HudApp.tsx` + `gui/webui/src/hud/*` — state machine over `pill / dot / card / toast / actionable / insight` overlays (`insight` = the compact post-capture coaching `InsightCard`, v3.10+), click-through on empty regions via `pointer-events: none`, FIFO queue for consent cards, max-3 visible toasts. Computes a `hasContent` flag (any pill / card / toast / actionable / insight / `demoMode`) and drives both the OS-level window visibility and a CSS opacity fade (`hud-fade-in` / `hud-fade-out`, `index.css::.hud-fade`, 180 ms ease-out) so the HUD softly fades in when content arrives and fades out before the host window disappears.

Toggle the entire system with `SAYZO_NOTIFICATIONS_ENABLED=0` (returns `NoopNotifier`, no HUD subprocess spawned). `[notify] ...` log shapes from `~/.claude/projects/.../memory/reference_notify_diagnostics.md` are preserved verbatim — every old triage script continues to work. The Windows AUMID set by the NSIS installer is still relevant for taskbar grouping but no longer load-bearing for notification rendering. macOS bundle signing is still required for the Microphone TCC dialog (capture-side) but no longer required for notifications.

**Testing the HUD without booting the agent:**
- `cd sayzo_agent/gui/webui && npm run dev:hud` — Vite HMR with a mock bridge; renders the HUD in a normal browser tab with `?demo=1` controls.
- `python scripts/preview_hud.py demo` — spawns the real frameless pywebview HUD subprocess with the in-window demo control strip. Use this for the focus-stealing regression check (open Zoom alongside, fire a `ConsentCard`, verify Zoom keeps input focus).
- `sayzo-agent diagnose-notifications` — exercises the end-to-end round-trip (toast + consent card + structured report) against a temporary HUD subprocess.

### Heartbeat log

`[heartbeat]` line every `Config.heartbeat_secs` seconds (default 30, `SAYZO_HEARTBEAT_SECS=0` disables). Shows arm state (`ARMED` + reason tag like `(zoom)` / `(hotkey)`, or `DISARMED`), detector state (`OPEN` / `PENDING_CLOSE` / `IDLE`), elapsed / silence counters, running kept/discarded counters. Lets a user watching the terminal for hours tell at a glance whether the agent is alive, what it's currently doing, and why it's currently armed.

## Architecture

The pipeline is **staged by cost** — cheap stages run continuously (while armed), expensive stages only run on data that survived the cheap gates. The arm model is layered on top: when disarmed, zero audio flows. When armed, the same pipeline the agent always had runs.

Transcription and speaker labels are server-side concerns now (Deepgram Nova-3 with `multichannel=true` + `diarize=true`). The agent uploads stereo OGG Opus (left=mic, right=system) and minimal metadata; the server fills in transcript / title / summary asynchronously and a background poller caches the title/summary back to local `record.json` for the Settings → Captures pane.

```
ArmController (DISARMED on launch)
    ↕ [hotkey press → start-confirm toast → arm]
    ↕ [whitelist match → consent toast → arm]
ArmController.arm() → vad.reset() + detector.reset_per_source_streams()
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
[AEC pre-pass — WebRTC AEC3 via livekit.rtc.apm; subtracts speaker bleed from
 mic at the sample level. Off by default in v3.5.0; SAYZO_AEC__ENABLED=1 to
 turn on. a later v3.5.x patch flips the default ON.]
    ↓
echo_guard (audio-energy classification; removes speaker-bleed segments from mic_segments)
    ↓
Cheap gate (substantive user turn rule)
    ↓ [whole session passes or whole session is dropped]
Post-capture DSP (highpass + spectral-gate denoise on mic; light HPF on system)
    ↓ [cleans the audio before encoding]
Slice both channels at [first VAD speech − pad, last VAD speech + pad] (preserves mid-conversation silence as recorded audio); zero mic_echo_segments spans on the mic channel only
    ↓
CaptureSink (Opus stereo: mic=L, system=R; record.json with synthetic placeholder title)
    ↓
UploadClient (POST /api/captures/upload, multipart audio + minimal record JSON, X-Agent-Version header)
    ↓ [on success, server response carries capture_id]
CapturePoller (background, GET /api/captures/{id}: caches title/summary into local record.json; on a LIVE capture with notify_capture_feedback ON, keeps polling to status=analyzed and fires the compact post-capture coaching-insight HUD card — or a fallback "saved" toast when no insight — deferring while the user is in another meeting)
```

Discard paths shrink to one: cheap-gate failure writes a `gate_failed` dropped-stub. Non-English language detection and empty-transcript checks were removed with the on-device STT cut — the server now decides what to do with multilingual / no-speech captures.

### Arm model (sayzo_agent/arm/)

`ArmController` in `arm/controller.py` is the single source of truth for armed state. Its background tasks (launched from `arm()`, cancelled on `disarm()`):

- **Whitelist watcher** (runs while DISARMED) — polls every `ArmConfig.poll_interval_secs` (default 2 s). Uses `platform_win.get_mic_holders()` / `platform_mac.is_mic_active()` + `get_foreground_info()` to build a `MicState` + `ForegroundInfo`. Feeds those to `detectors.match_whitelist()`. On match, fires the consent toast via `notifier.ask_consent()`. Per-app cooldown (30 min after decline, 10 min after session) keyed by `app_key`.
- **Long-meeting check-in task** (runs while ARMED) — sleeps until each `long_meeting_checkin_marks_secs` mark from session-start, fires "Still in the meeting?" toast. "Wrap up" → disarm with reason `CHECKIN_WRAP_UP`.
- **Meeting-ended watcher** (runs while whitelist-armed; NOT for hotkey-armed sessions) — polls mic-holders; if the arm-app hasn't held the mic for `whitelist_arm_release_grace_secs` (default 6 s = three absent polls at the 2 s interval), fires "Looks like your meeting ended" toast. "Keep going" snoozes `meeting_ended_snooze_secs` (default 10 min), then re-fires if still absent. Non-response defaults to Wrap up.

**Detection is mic-holder-based, not window-title-based.** `detectors.match_whitelist()` is pure logic operating on `MicState.holders`. Both platforms now populate this with real per-process mic-holders:
- **Windows**: pycaw WASAPI `IAudioSessionManager2` enumeration in `arm/platform_win.py`.
- **macOS** (v2.5+): `arm/audio-detect/main.swift` (CoreAudio `kAudioHardwarePropertyProcessObjectList`, macOS 14.4+) + Apple's responsibility SPI to map helper PIDs back to user-facing apps. The Python wrapper in `arm/audio_detect.py` shells out to it. Pre-v2.5 the macOS path was a foreground-coupled proxy (`mic_active_plus_running`) that required the meeting app to be the frontmost window — that constraint is GONE.

Works for Discord (which never changes window title during calls), survives app updates, mute-tolerant (muted users still have an active capture session), foreground-independent on both platforms.

**Capture scope (v2.9+).** Default is whole-endpoint system audio on every arm path (hotkey + whitelist auto-arm). The Settings → Recording → "Per-app audio capture (beta)" toggle (`CaptureConfig.system_scope=="arm_app"`) is the only knob that narrows scope, and when ON it applies to both: hotkey runs the whitelisted-holder matcher in `_resolve_hotkey_arm` (falls back to endpoint when no whitelisted holder is present, to avoid silent capture from Steam Voice / ChatGPT voice / Voice Recorder); whitelist auto-arm uses the meeting app's PIDs from `MatchResult.target_pids`. Mic device routing is independent of scope mode — always opportunistic to wherever the mic is being held. Don't re-introduce PID-scoping on the hotkey path while the toggle is off — the v2.x logs called it "smart-guess" but the result was always thrown away by the capture-layer safety-valve, and the misleading log line confused users into thinking we'd guessed wrong.

Default whitelist ships with 25 apps (14 desktop + 11 web — Meet/Teams-web/Zoom-web/Webex-web/Whereby/Jitsi/8x8 plus Discord/Slack/Skype/WhatsApp web counterparts of the desktop messaging apps) — see `config.py::default_detector_specs()`. Users edit the list via Settings → Meeting Apps (see `gui/webui/src/settings/MeetingAppsPane.tsx` + `AddAppDialog.tsx`, backed by `gui/settings/bridge.py`): toggle off / remove / one-click-add from a live mic-holder picker (desktop) or a pasted meeting URL (web). The Suggested-to-add section is driven by `arm/seen_apps.py`, which records any unmatched mic-holder the watcher observes while disarmed (capped at 20 entries). The in-app edit writes the full list to `user_settings.json` under `arm.detectors` and nudges the live agent over IPC to reload; `SAYZO_ARM__DETECTORS` env var still wins.

### Session state machine

`ConversationDetector` has three states:

- **IDLE** → no session. The ArmController calls `open_session_on_arm(now)` on every arm to transition into OPEN. Frames received while IDLE are **dropped on the floor** — there is no pre-buffer in armed-only mode (v2.1.7+); IDLE means "nothing should be coming through" and any frame that does is either a stale leftover from a previous arm cycle (e.g. `mic.queue` not fully drained) or post-close bleed-through, and either way it must not pollute the next session. The legacy `_open_session(now, trigger, vad_ts)` VAD-trigger path still exists as a fallback for unit tests that feed segments without frames.
- **OPEN** → session in progress. Joint silence ≥ `joint_silence_close_secs` transitions to…
- **PENDING_CLOSE** → buffers still held, nothing written to disk. `on_pending_close` callback (the ArmController) shows the end-confirmation toast:
  - `commit_close(reason)` — finalize: push buffers to `_closed_queue`, go back to IDLE, sink picks it up via `_ticker`.
  - `revert_close(now)` — cancel close: back to OPEN, silence timer reset.
  - VAD segment during PENDING_CLOSE → auto-revert (user resumed speaking is ground truth).
  - Legacy unit-test path (no callback registered) → commit immediately, preserving pre-armed-model behavior.

**Gap-fill cap + stale-frame guard (v3.6+).** `on_frame` zero-fills dropped-frame and cold-start gaps to preserve the sample-to-mono-time invariant — load-bearing for AEC, since mic[k] and sys[k] must represent the same wall-clock moment or the ±500 ms cross-correlation search can't find the alignment. The cap is `ConversationConfig.max_gap_fill_secs` (default 30 s in v3.6+, was 2 s pre-v3.6). Above the cap, a pathological gap (literal system suspend, USB reconnect after minutes) re-anchors with a small audible discontinuity rather than injecting tens of seconds of zeros. Stale frames (`capture_mono_ts < session_t0_mono - 0.5 s`) — leftovers from a previous arm cycle that leaked through the producer queue — are detected explicitly at the top of `on_frame` and dropped; pre-v3.6 the re-anchor branch did double duty as the stale-frame guard with a tight 2 s cap, so legitimate WASAPI cold-start delays (1–5 s typical, ~3.5 s seen in the wild) silently hit re-anchor without zero-fill and misaligned mic vs sys by the startup delay. The visible symptom was AEC `peak < 0.05` on every capture and ~0 dB cancellation — fixed in v3.6.0.

New `SessionCloseReason` values: `HOTKEY_END`, `CHECKIN_WRAP_UP`, `WHITELIST_ENDED`. `SAFETY_CAP` is kept in the enum for backward compat with on-disk records but nothing in the new code path emits it (the 60-min cap is removed).

### Critical design rules (do not regress these)

These are the rules the conversation detector and gate logic encode. Several were added in response to specific failure modes the user identified during planning — re-introducing any of them silently breaks the product's core value.

1. **Sessions are bounded by sustained silence, not by fixed time windows.** The detector must NOT require mic and system speech to co-occur inside a rolling window. A 10-minute user demo with one "thanks" at the end is a single session and must be captured whole. Same for the inverse (long other-side monologue + short user reply).

2. **The substantive-user-turn rule lives in `evaluate_user_turn_gate`** in `conversation.py`. Single-threshold rule (v3.5.2+): a session passes iff cumulative mic voiced time ≥ `min_user_total_secs` (default 8 s), however distributed — one long turn, many short turns, doesn't matter. **The user's substantive speech may come anywhere in the session — including at the very end.** Discarding a session early because the first user turn was an "mhm" is a bug. The pre-v3.5.2 dual-path (one 8s single turn OR 10s cumulative across ≥2 turns) was removed because the AND inside the cumulative branch surprised users into thinking 8s+ of short bursts should pass when it didn't.

3. **When a session passes the gate, the *whole* session uploads** — no trimming a slice "to save bytes." The server's Deepgram pass needs the surrounding other-side speech as context for diarization; the agent's job is to deliver a faithful stereo recording, not a curated excerpt.

4. **Transcript, speaker labels, title, summary are server-side concerns.** The agent writes a local placeholder title (`"Conversation · YYYY-MM-DD HH:MM"` or `"Zoom call · ..."` when the arm-app is known) into `record.json` so the Settings → Captures pane has something readable immediately. The upload payload (`serialize_record_for_upload`) carries only `id` / `started_at` / `ended_at` / `metadata.close_reason` plus the audio blob. `CapturePoller` overwrites the local placeholder title once the server reports `status≥transcribed`. Don't reintroduce on-device STT or speaker embedding — the agent is meant to stay small (~50 MB idle, no GB-class model loads).

5. **AEC + echo_guard are complementary; neither alone is sufficient.** `aec.cancel_echo` (WebRTC AEC3, shipped opt-in in v3.5.0, default ON since v3.6.1) is the linear cancellation layer — adaptive filter that learns the speaker→mic impulse response from the system reference and subtracts predicted echo from the mic at the sample level. `echo_guard.classify_buffers` is the non-linear residual safety net — energy-based per-segment classification (Welch coherence + Wiener-residual speech probability) catches what AEC's linear filter can't model: cheap-laptop speaker driver compression, Bluetooth codec re-encoding artifacts, reverb tails that exceed AEC3's impulse-response window. The server's `isEchoLeakUtterance` pairs with both. Don't remove either — they target different leak shapes. **AEC alone leaves a non-linear residual during double-talk regions** (when user is speaking AT THE SAME TIME as the other party, AEC3 freezes adaptation to avoid learning the user's voice as echo); the residual is loud enough that Deepgram occasionally transcribes it as user speech. v3.6.x onward layers the server's `isEchoLeakUtterance` + agent-side echo_guard residual classification to catch this — neither layer alone is sufficient.

6. **mic↔sys buffer alignment is load-bearing for AEC.** `mic_pcm[k]` and `sys_pcm[k]` must represent the same wall-clock instant for every k, or AEC3's ±500 ms cross-correlation lag search finds noise instead of the real echo path. The sample-to-mono-time invariant is maintained by `on_frame` zero-filling cold-start gaps between `session_t0_mono` and the first frame on each source (mic and sys capture threads start asynchronously and their first-frame latencies differ by seconds — sys typically lags mic by 2–4 s on a fresh WASAPI open). Pre-v3.6 `max_gap_fill_secs=2.0` was too tight, and the re-anchor branch silently skipped the zero-fill for any startup delay above the cap, misaligning every session. The v3.6 default is 30 s. The regression tests in `test_conversation.py::test_first_*_frame_after_startup_delay_zero_fills` + `test_mic_and_sys_pcm_aligned_after_asymmetric_startup` are the guard — don't drop them, and re-run them after any change to `on_frame`.

### Module responsibilities

- **`conversation.py`** — Pure (no I/O, no models). State machine (IDLE / OPEN / PENDING_CLOSE) + gate. Unit-tested with synthetic VAD events. The most behavior-critical file in the project; treat changes here with care.
- **`app.py`** — Async orchestrator. Wires ArmController → capture → VAD → detector → heavy-worker pool → sink → upload → poller. `_consume` waits on `arm.armed_event` so frames only flow while armed. Echo_guard + DSP + Opus encoding run on a single-worker `ThreadPoolExecutor` so they never starve the asyncio loop. Capture + VAD run on the loop. The placeholder title is generated by the sink at write time from `buffers.arm_app_key` / `buffers.arm_app_display` + `started_at`.
- **`arm/controller.py`** — ArmController state machine. Owns stream lifecycle, hotkey confirmations, whitelist consent, PENDING_CLOSE handling, long-meeting check-ins, meeting-ended watcher. Unit-tested with fake capture / VAD / notifier / injected platform queries.
- **`arm/detectors.py`** — Pure matching logic against `DetectorSpec` list from config. No OS calls. Skips specs with `disabled=True` (the Meeting Apps pane's off toggle).
- **`arm/seen_apps.py`** — Persistence for unmatched mic-holders observed by the whitelist watcher. Written to `data_dir/seen_apps.json`. Capped at 20 entries, dedup'd by lower-cased key, scrubbed against the current whitelist on read. Drives the Settings → Meeting Apps "Suggested to add" section.
- **`arm/platform_win.py` / `arm/platform_mac.py`** — Platform-specific OS queries:
  - Windows: pycaw for mic-session enumeration (PIDs + process names), `win32gui` for foreground, UIAutomation for browser tab URLs.
  - macOS (v2.5+): shells out to the `audio-detect` Swift helper for per-process mic attribution (PIDs + bundle ids, mapped through the responsibility SPI). NSWorkspace for foreground, AX (`AXUIElementCopyAttributeValue` for `kAXTitleAttribute` + `AXURL` on `AXWebArea`) for browser window titles + URLs. AppleScript was deliberately abandoned — it triggered the alarming "Sayzo wants to control your browser" Automation TCC dialog.
- **`arm/audio-detect/main.swift`** — macOS-only Swift helper, ~250 lines, compiled in CI. Read-only enumeration of `kAudioHardwarePropertyProcessObjectList` (macOS 14.4+) plus the responsibility SPI (`responsibility_get_pid_responsible_for_pid`, the same source the orange privacy indicator uses). Zero permissions required (no `NSAudioCaptureUsageDescription`, no Microphone, no Screen Recording — strictly observation). Outputs JSON via `--json`.
- **`arm/audio_detect.py`** — Python wrapper around the above. Locates the binary (frozen-bundle path → dev path → `$PATH`), runs `--json` per call with a 1 s cache to avoid hammering subprocess on the 2 s watcher poll. Returns one `AudioProcess` per row with PID, responsible_pid, bundle_id, input/output/running flags.
- **`arm/hotkey.py`** — pynput global hotkey listener. Marshals key-press events from pynput's listener thread onto the arm controller's asyncio loop via `call_soon_threadsafe`. Requires Accessibility permission on macOS (graceful fallback: tray menu still works).
- **`notify.py`** — Notifier Protocol (`notify`, `ask_consent`, `notify_actionable`, `notify_insight`, `has_authorisation_sync`) + `HudNotifier` (wraps `HudLauncher`, see `gui/hud/`) + `NoopNotifier` (silent fallback for tests / `SAYZO_NOTIFICATIONS_ENABLED=0`). `notify_insight` (v3.10+) renders the compact post-capture coaching card; it shares the launcher's `_pending_actionables` callback map + dispatch with `notify_actionable` (request-id prefixes `insight-` / `actionable-` never collide). Every notification routes through the HUD subprocess — see the "Notifications: custom HUD overlay" section above for the full architecture.
- **`gui/hud/`** — custom HUD subsystem. `launcher.py` (parent-side subprocess manager + JSON stdin/stdout + per-request `Future` dispatch for `ask_consent`), `window.py` (child-side pywebview window + platform overlay tweaks), `bridge.py` (JS-callable `hud_event` method). Pairs with React components at `gui/webui/src/HudApp.tsx` + `gui/webui/src/hud/*`.
- **`settings_store.py`** — JSON load/save for `data_dir/user_settings.json`. The Settings GUI + onboarding write here; `load_config` overlays onto `ArmConfig` defaults (env vars still win over JSON).
- **`pidfile.py`** — Cross-platform single-instance enforcement via **kernel-level locks** (v2.7.1+). Windows: named mutex via `CreateMutexW(L"Local\\Sayzo-Lock-<hash>")` — the mutex name is hashed off the absolute pidfile path so each install + each test ``tmp_path`` gets its own lock. macOS / Linux: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on the pidfile itself. The kernel auto-releases on process death (clean exit, kill, BSOD, **reboot**) so there is no userspace state that can be stale. The `.pid` file still exists but is purely informational (current primary's PID for IPC routing + diagnostics) — `is_running()` consults the kernel lock, not the file. Replaced the v2.1.18/2.1.19 `O_EXCL`+`psutil.pid_exists` scheme after a v2.7.0 user report where post-reboot PID recycling locked every Sayzo launch out (`psutil.pid_exists` returned True for the recycled PID, `try_acquire_pidfile` thought another instance was running, every Task Scheduler / Start Menu launch silently exited).
- **`gui/settings/lockfile.py`** — Settings subprocess uses the same `pidfile.try_acquire_pidfile` primitive against `data_dir/settings.pid` so the Settings GUI inherits identical kernel-lock guarantees. Thin context-manager wrapper.
- **`comtypes_setup.py`** — Redirects comtypes' runtime stub cache from `%TEMP%/comtypes_cache/<exe>-<py>` (volatile — Storage Sense, AV scanners, profile resets blow it away → unhandled exception on next launch) to `data_dir/comtypes_cache` (stable, owned by us). Defense in depth — CI's `scripts/prebake_comtypes.py` runs `comtypes.client.GetModule("UIAutomationCore.dll")` + `stdole2.tlb` before PyInstaller so the bundle ships pre-generated `comtypes.gen.UIAutomationClient` etc. as static .py files and the runtime cache rarely fires at all. Called from `service()` / `run()` in `__main__.py` immediately after logging setup, before any pycaw / uiautomation import. **Don't reintroduce `%TEMP%`-cached COM type-library generation** — it was the root cause of "unhandled exception on first launch after a long-running profile" reports.
- **`__main__.py::_install_excepthooks`** — Routes unhandled `sys.excepthook` + `threading.excepthook` exceptions to `agent.log` via `log.critical(..., exc_info=...)` *before* the default handler runs. Without this, windowed-exe stderr is `/dev/null`, so the user sees a generic OS "unhandled exception" dialog and we get no traceback to debug from. Installed in both `service()` and `run()` immediately after the logging setup. Don't remove — every weird startup crash report depends on this for postmortem.
- **`capture/system.py`** — Uses PyAudioWPatch for WASAPI loopback capture. Captures at the device's native sample rate (typically 48 kHz) and resamples to 16 kHz via scipy to avoid quality loss.
- **`aec.py`** — WebRTC AEC3 pre-pass. Wraps `livekit.rtc.apm.AudioProcessingModule` (Apache-2.0, same engine as Chrome/Meet). Runs at session close on the heavy-worker executor, BEFORE `echo_guard`. Iterates the mic+sys buffers in 10 ms frames at 16 kHz mono (160 samples each), feeding `process_reverse_stream(sys)` then `process_stream(mic)`; the mic frame is mutated in place and concatenated into the cleaned mic. Global mic↔sys lag is estimated once via `echo_guard.estimate_delay` and used to pre-shift sys before the frame loop, so AEC3's internal delay tracker only handles per-frame jitter. Lazy-imports the heavy livekit FFI binary to preserve v2.14 boot perf. Off by default in v3.5.0; flip via `SAYZO_AEC__ENABLED=1`.
- **`echo_guard.py`** — Pure numpy/scipy energy classifier. Operates on `SessionBuffers` mic + sys PCM, drops mic VAD segments that correlate strongly with the system channel (Welch coherence + Wiener residual). Removed segments land in `buffers.mic_echo_segments`. When AEC runs first (v3.5.0+, opt-in), echo_guard operates on already-linearly-cleaned mic and serves as the non-linear residual safety net. Server-side `isEchoLeakUtterance` pairs with both; see Critical design rule 5.
- **`sink.py`** — Persists `ConversationRecord` (id, timestamps, synthetic placeholder title, empty summary, metadata) to `record.json` and encodes mic+system as a single stereo Opus blob (`audio.opus`, left=mic, right=system). Also caches three agent-side fields into `record.metadata` at write time — `local_clock_label` ("2:30 pm" — TZ locked at capture moment, not display moment), `arm_app_key`, and `arm_app_display` — so `CapturePoller._source_label` can derive the insight card's source-anchor chip deterministically without depending on the server's later title pass. Placeholder title now prefers `arm_app_display` ("Microsoft Teams call · …") over `arm_app_key.title()` which produced gross strings like "Teams_Desktop" / "Gmeet". Exposes two serializers: `serialize_record` (full local schema for disk) and `serialize_record_for_upload` (minimal subset for the multipart POST body — no title, summary, transcript, or local-only metadata). Also exports `local_clock_label(ts)` as a pure helper.
- **`capture_poller.py`** — Background polling for the server's late-arriving title/summary **and** the post-capture coaching insight (v3.10+). After `UploadRetryManager` sees an `UploadOutcome.SUCCESS`, it fires `CapturePoller.poll(rec_dir, capture_id, owns_toast)` as a fire-and-forget asyncio task (`owns_toast` = live capture AND `notify_capture_feedback` on). Two modes: **`owns_toast=False`** (sweep re-uploads / feature off) — legacy behavior: cache title/summary, stop on first cached title or terminal status, no toast. **`owns_toast=True`** — keeps polling to `status=="analyzed"` (the only point the server populates/trusts `coaching_insight`; backoff schedule reaches ~28 min, re-tune when the server ships `analyzedAt`), validates + persists the insight to `record.json::metadata.coaching_insight`, then fires the compact InsightCard via `notifier.notify_insight` ("See full feedback" deep-link + "Stop showing these" off-switch), or a fallback "Capture saved" toast when no insight is produced. Defers the fire while `armed_check()` reports another meeting in progress (drops after a 1 h staleness cap). The card's "Stop showing these" flips `cfg.notify_capture_feedback` + persists to `user_settings.json` in-process. No restart persistence — if the agent crashes mid-poll the placeholder + any pending insight are lost; the webapp re-renders the same insight as a hero card on the deep-link target. The card's source-anchor chip ("from your 2:30 pm Zoom call" / "from your 2:30 pm conversation") is derived by `_source_label` from `metadata.arm_app_display` + `metadata.local_clock_label` cached at sink time — never from `record.title`, so the chip's wording stays deterministic regardless of whether the server's title pass succeeded. Freshness chip text ("Just now" / "5 min ago" / "1 hr ago") is computed by `_freshness_label` at fire time from `record.ended_at`, inside the `fire()` closure so deferred fires don't claim "Just now" for an hour-old capture.
- **`upload_retry.py`** — Owns per-record retry state in `metadata.upload` and the global pause sidecar (`.upload_state.json`). On success spawns the poller via the injected `on_upload_success(rec_dir, capture_id, owns_toast)` hook, where `owns_toast = live AND feedback_enabled()`. Toast wiring (the "Capture saved to Sayzo" actionable) gated on `Config.notify_capture_saved` AND `try_upload(live=True)` AND **NOT** `feedback_enabled()` — when the post-capture feedback feature owns the toast (`owns_toast`), the immediate saved toast is suppressed and the poller fires the single per-capture toast instead (insight card, or its own fallback saved toast). The two decisions are complementary so a capture never gets two toasts and never zero-by-race (`feedback_on` is read once per attempt). Sweep successes (automatic backlog drain + user-triggered Try Again in Settings → Captures) stay silent so a backlog of "Couldn't upload" captures clearing doesn't spam a burst of toasts; visible feedback for sweep success is the Captures pane row flipping out of the failed state.
- **`session_trim.py`** — Pure numpy/bytes helper. `apply_session_trim` runs after DSP, before sink. Slices `mic_dsp` and `sys_dsp` at `[first_speech - pad, last_speech + pad]` using identical sample indices on both channels (alignment is load-bearing — see Critical design rule 6). On the mic channel only, zeros any `mic_echo_segments` spans inside the kept range. Mid-conversation silences (thinking pauses, response latency) survive as recorded audio. Returns a `TrimReport` that lands in `record.json::metadata.trim`. Replaces the pre-v3.7 "windowing + trailing-trim" model (which zero-filled gaps > 5 s and was a confusing fit for echo defense).
- **`dsp.py`** — Pure numpy/scipy post-processing applied at session close, before Opus encoding. Butterworth highpass + `noisereduce` spectral-gate denoise (default `prop_decrease=0.5`, dialed down from 0.85 to avoid phasey artifacts) + peak-normalize on mic; light highpass + peak-normalize on system (no denoise — system audio is typically a clean digital stream already, and aggressive denoising damages music / low-volume speech from the far side). Peak-normalize (v3.6.4+) targets −3 dBFS with a **6 dB max-gain cap**: when AEC cancels strongly the post-AEC peak drops, and an uncapped normalize would apply 15–20 dB of gain to reach the target — amplifying constant background (fan hum, room tone) into audibility along with the legitimate signal. The cap keeps amplification proportional to AEC's own reduction so quiet-input captures emit below target rather than getting pathologically lifted. Runs on the heavy-worker executor. All stages are config-flagged under `CaptureConfig` — `SAYZO_CAPTURE__DSP_ENABLED=0` restores raw-PCM output byte-for-byte (minus the encoder's `application` setting, which is intrinsic to the sink path). Opus encoder knobs also live in `CaptureConfig` (`opus_bitrate`, `opus_application`). Default is `application="audio"` at 96 kbps stereo — transparent for speech, usable for music. Capture modules no longer apply per-batch RMS normalization (previously `_TARGET_RMS=0.02` caused audible pumping); raw captured levels flow through and DSP's peak-normalize at session close handles final loudness.

## Known tech debt

Nothing currently blocking. Production distribution is solid: Windows users get a signed NSIS installer; macOS users get a Developer-ID-signed + Apple-notarized DMG (the CI workflow at `.github/workflows/build.yml` does the signing + `xcrun notarytool` + `xcrun stapler` on every push). The `installer/install.sh` and `install.ps1` one-liners pull from the auto-update host. Auto-update via `latest.json` ships every release.
