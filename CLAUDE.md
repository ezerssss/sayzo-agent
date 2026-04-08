# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Python agent that runs 24/7 on a user's machine, listens to mic + system audio, detects when the user is **actively participating** in a real conversation, and writes a transcript + compressed audio file to disk. It is the data-collection arm of Eloquy, an English coaching platform — the captured conversations feed server-side analysis that drives personalized speaking drills. Server upload is currently a no-op stub (`NoopUploadClient`).

Everything must run locally (no paid APIs in the hot path) and must be cheap enough to run 24/7 in the background.

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

**Python 3.13 does not work** — `llama-cpp-python` has no prebuilt wheels for it. Stay on 3.12.

`resemblyzer` will print a `pip` warning that `webrtcvad` and `typing` are missing. Both are harmless: `webrtcvad-wheels` provides the same `webrtcvad` Python module under a different distribution name, and Python 3.12 has `typing` built in.

## Common commands

```bash
# Pure unit tests (no model loading, no audio I/O — fast)
pytest tests/

# Single test
pytest tests/test_conversation.py::test_gate_passes_late_substantive_user_turn -v

# CLI commands (all under one entrypoint)
eloquy-agent setup         # one-time: download Qwen GGUF (~2 GB) into ~/.eloquy/agent/models/
eloquy-agent enroll        # one-time: record voiceprint to ~/.eloquy/agent/voiceprint.npy
eloquy-agent devices       # list mic + loopback devices
eloquy-agent test-capture  # 10-second capture sanity check
eloquy-agent run           # main 24/7 loop with verbose terminal output
```

Captures land in `./eloquy-data/captures/<id>/` (project-local by default, override with `ELOQUY_DATA_DIR`) as `record.json` + `audio.opus`.

### Persistence vs. upload (don't get these confused)

`CaptureSink.write` in `sink.py` is what persists a kept session to disk. It runs **before** `UploadClient.upload`. `NoopUploadClient` is a no-op — it only logs `"[upload] (noop) …"`. So even though server upload is a stub, every session that survives the pipeline is already saved locally and survives restarts. The sink logs absolute paths of the written `record.json` and `audio.opus` so you can see exactly where they landed. If you ever wire up a real `UploadClient`, do not delete the local files on upload success — the sink write is the source of truth.

### Heartbeat log

The main loop emits a `[heartbeat]` line every `Config.heartbeat_secs` seconds (default 30, override via `ELOQUY_HEARTBEAT_SECS`, set to 0 to disable). It shows current state (`IDLE` / `OPEN`), pre-buffer fill or in-session voiced time, LLM loaded/unloaded, and running kept/discarded counters. This exists so a user watching the terminal for hours can tell at a glance that the agent is alive and what it's doing, instead of assuming it crashed during long silent periods.

## Architecture

The pipeline is **staged by cost** — cheap stages run continuously, expensive stages only run on data that survived the cheap gates. This is the central design decision and it's load-bearing for the 24/7 resource budget.

```
mic + system capture (asyncio queues, 16 kHz mono)
    ↓ [always on]
Silero VAD (per source, stateful)
    ↓ [emits SpeechSegment events]
ConversationDetector (silence-bounded sessions)
    ↓ [appends PCM + segments to session buffer]
[session closes on sustained joint silence OR safety cap]
    ↓
Cheap pre-STT gate (substantive user turn rule)
    ↓ [whole session passes or whole session is dropped]
faster-whisper (transcribe mic + system separately)
    ↓
Speaker tagging (Resemblyzer cosine vs voiceprint + greedy clustering for others)
    ↓
Relevance LLM (Qwen 2.5 3B Q4 via llama-cpp-python)
    ↓ [judges participant + extracts relevant span + title + summary]
CaptureSink (Opus stereo: mic=L, system=R; record.json)
    ↓
UploadClient (NoopUploadClient for now)
```

### Critical design rules (do not regress these)

These are the rules the conversation detector and gate logic encode. Several were added in response to specific failure modes the user identified during planning — re-introducing any of them silently breaks the product's core value.

1. **Sessions are bounded by sustained silence, not by fixed time windows.** The detector must NOT require mic and system speech to co-occur inside a rolling window. A 10-minute user demo with one "thanks" at the end is a single session and must be captured whole. Same for the inverse (long other-side monologue + short user reply).

2. **The substantive-user-turn rule lives in `evaluate_user_turn_gate`** in `conversation.py`. A session passes only if the user has either (a) one continuous turn ≥ `min_user_turn_secs` (default 8 s), OR (b) cumulative voiced time ≥ `min_user_total_secs` over ≥ `min_user_turns_for_total` distinct turns. **The user's substantive turn may come anywhere in the session — including at the very end.** Discarding a session early because the first user turn was an "mhm" is a bug.

3. **When a session passes the gate, the *whole* session is transcribed**, not a trimmed slice. The other-side speech surrounding the user's turns is required context for downstream analysis.

4. **The relevance LLM must NOT crop tightly.** Its prompt instructs it to be generous. As a safety net, `RelevanceLLM._parse` also pads `relevant_span` by ±15 s (`SPAN_PAD_SECS`) before returning. Small local LLMs crop too tight even when told not to — the padding is intentional.

5. **Whisper hallucinates "Thank you" / "Thanks for watching" on silence.** Mitigated in `WhisperSTT.transcribe_pcm16` by `vad_filter=True`, `condition_on_previous_text=False`, and tightened `no_speech_threshold` / `log_prob_threshold`. Don't loosen these.

### Module responsibilities

- **`conversation.py`** — Pure (no I/O, no models). State machine + gate. Unit-tested with synthetic VAD events. The most behavior-critical file in the project; treat changes here with care.
- **`app.py`** — Async orchestrator. Wires capture → VAD → detector → heavy-worker pool → sink. All STT/embedding/LLM work runs on a single-worker `ThreadPoolExecutor` so heavy stages never run in parallel and starve the CPU. Capture + VAD run on the asyncio loop.
- **`relevance.py`** — Loads Qwen lazily on first use, unloads after `idle_unload_secs` (default 5 min) to free ~2 GB of RAM during idle periods. The system prompt is the contract — modifying it changes the JSON shape downstream.
- **`capture/system.py`** — Contains a numpy 2.x compatibility shim (`np.fromstring = np.frombuffer`) for the unmaintained `soundcard` package. Must be applied **before** `import soundcard`. Don't move the shim.
- **`speaker.py`** — Greedy cosine clustering for other-speaker labels (avoids a sklearn dependency). Heavy imports (`resemblyzer`) are lazy so unit tests don't need them.

## Distribution caveats (future work)

The current install is fragile by design — it's a dev install, not a distributable. If/when shipping to non-dev users:

- `soundcard` is unmaintained and broken on numpy 2.x → replace with `pyaudiowpatch` (Windows) or platform-specific loopback.
- `resemblyzer` forces source-build of `webrtcvad` → replace with a directly-loaded ONNX speaker encoder.
- `llama-cpp-python` wheels are Python-version-fragile → bundle via PyInstaller/Nuitka instead of relying on pip.
- Models are downloaded post-install via `huggingface_hub` (cached, idempotent) — this part is fine to keep.

The plan in `~/.claude/plans/linear-squishing-bird.md` (referenced from session history) has the full rationale.
