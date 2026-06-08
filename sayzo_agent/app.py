"""Async orchestrator wiring all pipeline stages."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Optional

import numpy as np

from .arm import ArmController
from .capture.mic import MicCapture
from .capture import SystemCapture
from .capture_poller import CapturePoller
from .config import Config
from .conversation import (
    ConversationDetector,
    SessionState,
    evaluate_user_turn_gate,
)
from .dsp import apply_mic_dsp, apply_sys_dsp
from . import aec, echo_guard
from .models import SessionBuffers
from .session_trim import apply_session_trim
from .sink import CaptureSink
from .notify import NoopNotifier, Notifier
from .retry import empty_upload_state
from .upload import NoopUploadClient, UploadClient
from .upload_retry import UploadRetryManager
from .vad import SileroVAD

log = logging.getLogger(__name__)


def _format_duration(secs: float) -> str:
    """Format a duration as a short, human-friendly string ("12s", "1 min")."""
    if secs >= 60:
        return f"{int(round(secs / 60))} min"
    return f"{int(round(secs))}s"


# --- HUD waveform audio-level normalization ---------------------------------
# Frames arrive at 50 Hz (20 ms each, see capture/mic.py::frame_ms). Peaks
# slow-decay so a brief silence doesn't immediately drag the peak down to
# zero; a multi-second silence eventually does. Half-life ≈ ln(0.5)/ln(decay)
# / 50 fps → with 0.995 that's ~2.8 s. MIN_PEAK_* is the denominator floor:
# during true silence the peak decays toward zero, and dividing tiny ambient
# RMS by tiny peak would amplify noise to 100% bars; the floor pins
# silence-vs-noise correctly.
_AUDIO_LEVEL_DECAY = 0.995
_AUDIO_LEVEL_INIT_PEAK_MIC = 0.05
_AUDIO_LEVEL_INIT_PEAK_SYS = 0.02
_AUDIO_LEVEL_MIN_PEAK_MIC = 0.015
_AUDIO_LEVEL_MIN_PEAK_SYS = 0.004




class Agent:
    def __init__(
        self,
        config: Config,
        upload_client: Optional[UploadClient] = None,
        mic_capture=None,
        sys_capture=None,
        notifier: Optional[Notifier] = None,
        auth_client=None,
    ) -> None:
        self.cfg = config
        self.mic = mic_capture or MicCapture(
            sample_rate=config.capture.sample_rate,
            frame_ms=config.capture.frame_ms,
            device=config.capture.mic_device,
        )
        self.sys = sys_capture or SystemCapture(
            sample_rate=config.capture.sample_rate,
            frame_ms=config.capture.frame_ms,
            device=config.capture.sys_device,
            system_scope=config.capture.system_scope,
            silence_pump_enabled=config.capture.system_silence_pump_enabled,
        )
        self.vad_mic = SileroVAD(
            "mic",
            threshold=config.vad.threshold,
            min_speech_ms=config.vad.min_speech_ms,
            hangover_ms=config.vad.hangover_ms,
        )
        self.vad_sys = SileroVAD(
            "system",
            threshold=config.vad.threshold,
            min_speech_ms=config.vad.min_speech_ms,
            hangover_ms=config.vad.hangover_ms,
        )
        self.detector = ConversationDetector(config.conversation, sample_rate=config.capture.sample_rate)
        self.sink = CaptureSink(
            config.captures_dir,
            opus_bitrate=config.capture.opus_bitrate,
            opus_application=config.capture.opus_application,
        )
        self.upload = upload_client or NoopUploadClient()
        self.notifier: Notifier = notifier or NoopNotifier()

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sayzo-heavy")
        self.poller = CapturePoller(
            auth_client=auth_client,
            captures_dir=config.captures_dir,
            executor=self._executor,
            notifier=self.notifier,
            config=config,
            # Defer a post-capture insight when the user is in ANOTHER meeting
            # at fire time. ``self.arm`` is constructed further down, so read
            # it lazily — this callable only runs during a poll, well after
            # __init__ completes.
            armed_check=lambda: bool(
                getattr(self, "arm", None) is not None
                and self.arm.armed_event.is_set()
            ),
        )
        self.retry_mgr = UploadRetryManager(
            captures_dir=config.captures_dir,
            upload_client=self.upload,
            notifier=self.notifier,
            executor=self._executor,
            config=config.upload,
            auth_client=auth_client,
            webapp_base_url=config.auth.effective_server_url or None,
            on_upload_success=self.poller.poll,
            notify_capture_saved=config.notify_capture_saved,
            # Live read so a Settings toggle / in-card opt-out applies without a
            # restart (the IPC reload mutates this same ``config`` object).
            feedback_enabled=lambda: config.notify_capture_feedback,
        )
        self._stop = asyncio.Event()
        self._paused = asyncio.Event()  # clear = running, set = paused
        self._processing_tasks: set[asyncio.Task] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._heartbeat_last: float = 0.0
        self._upload_sweep_last: float = 0.0
        self._sweep_in_progress: bool = False
        self._captures_kept: int = 0
        self._captures_discarded: int = 0
        self._echo_segments_dropped: int = 0
        self._echo_secs_dropped: float = 0.0
        # Per-source NORMALIZED audio levels (not raw RMS). Computed in
        # `_consume`: each frame's RMS is divided by a slow-decaying
        # per-source peak so a quiet mic and a loud mic both fill the
        # bars during speech and silence reads as silence. Drained at
        # ~15 Hz by `_audio_level_emitter` and pushed to the HUD pill's
        # waveform. Plain floats — Python GIL makes the load/store
        # atomic enough for telemetry-grade smoothness; we don't lock.
        self._latest_mic_level: float = 0.0
        self._latest_sys_level: float = 0.0
        # Slow-decaying peaks (one per source) that the normalizer
        # divides by. Initial seeds picked so the FIRST frame of a new
        # session doesn't normalize ambient noise to full scale; the
        # decay + min-floor below stabilize them within a few seconds.
        self._mic_peak: float = _AUDIO_LEVEL_INIT_PEAK_MIC
        self._sys_peak: float = _AUDIO_LEVEL_INIT_PEAK_SYS
        # Settings → Captures pane reads this via IPC. Keys are the proc_id
        # (also reused as the eventual on-disk record id), values describe
        # what's currently being processed for the user-facing list.
        self._processing_state: dict[str, dict] = {}

        # Armed-only model: the ArmController owns mic + system stream
        # lifecycle (start on arm, stop on disarm), hotkey confirmations,
        # whitelist consent toast, PENDING_CLOSE end-confirmation, long-
        # meeting check-ins, and meeting-ended watcher. Agent.run() defers
        # all capture-start decisions to it.
        self.arm = ArmController(
            self.cfg.arm,
            self.detector,
            mic_capture=self.mic,
            sys_capture=self.sys,
            vad_mic=self.vad_mic,
            vad_sys=self.vad_sys,
            notifier=self.notifier,
            data_dir=self.cfg.data_dir,
            system_scope_fn=lambda: self.cfg.capture.system_scope,
            show_recording_indicator_fn=lambda: self.cfg.hud.show_recording_indicator,
        )

    # ---- pipeline ----------------------------------------------------------

    async def _consume(self, source: str, queue: asyncio.Queue, vad: SileroVAD) -> None:
        """Drain one capture queue while the agent is armed.

        Disarmed means: no capture streams are open (ArmController.disarm
        stopped them), so ``queue`` won't receive new frames. We block on
        ``armed_event`` to avoid busy-spinning, and use a short timeout on
        ``queue.get()`` so the disarm transition is noticed even if a few
        frames remain buffered.
        """
        # Local arm-transition tracking so each fresh arm cycle starts
        # with the seed peak — otherwise the previous session's residual
        # peak (or worse, a peak that decayed to MIN during a long quiet
        # tail) would warp the first few frames of the new session.
        was_armed = False
        while not self._stop.is_set():
            if self._paused.is_set():
                await asyncio.sleep(0.5)
                continue
            # Armed gate — block here while disarmed. ArmController sets the
            # event when streams are open and producing frames.
            try:
                await asyncio.wait_for(self.arm.armed_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                was_armed = False
                continue
            if self._stop.is_set():
                break
            if not was_armed:
                # Disarmed → armed transition: reset this source's peak so
                # the normalizer doesn't inherit stale state.
                if source == "mic":
                    self._mic_peak = _AUDIO_LEVEL_INIT_PEAK_MIC
                else:
                    self._sys_peak = _AUDIO_LEVEL_INIT_PEAK_SYS
                was_armed = True
            # Queue payload is a (capture_mono_ts, frame) tuple. Capture-
            # stamping happens at the hardware boundary in each capture
            # module (sounddevice callback / WASAPI read / audio-tap
            # header); the detector uses `capture_mono_ts` to keep mic and
            # system buffers aligned to a shared session timeline.
            try:
                capture_mono_ts, frame = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # re-check armed_event in case we disarmed
            now = time.monotonic()
            try:
                self.detector.on_frame(source, frame, capture_mono_ts, now)
                for seg in vad.feed(frame, capture_mono_ts):
                    self.detector.on_segment(seg, now)
            except Exception as exc:
                # An unhandled throw here used to silently kill the
                # consume task: the agent kept running (heartbeat,
                # hotkey, tray) while the capture queue filled forever
                # (QueueFull spam, 0 s captures, gate_failed). Log
                # critical + signal stop so the failure is visible.
                # `_prewarm_vads` already validates VAD load at boot,
                # so reaching this branch in production means something
                # genuinely unexpected went wrong (e.g. a corrupted
                # frame from a misbehaving driver) — full shutdown is
                # safer than continuing in a half-broken state.
                log.critical(
                    "[agent] %s consume crashed (%s: %s) — shutting "
                    "down so the failure surfaces instead of silently "
                    "discarding sessions.",
                    source, type(exc).__name__, exc,
                    exc_info=True,
                )
                self._stop.set()
                return
            # Per-frame RMS + per-source slow-peak normalization for the
            # HUD waveform. Frames are float32 in [-1.0, 1.0] (see
            # capture/mic.py and capture/system_win.py). Normalization
            # makes a quiet laptop mic and a loud Blue Yeti fill the bars
            # the same way during speech without per-machine tuning; the
            # MIN_PEAK floor prevents true silence from being amplified
            # to full scale by a peak that's decayed near zero.
            if frame.size:
                try:
                    rms = float(np.sqrt(np.mean(frame * frame, dtype=np.float64)))
                except Exception:
                    rms = 0.0
                if source == "mic":
                    if rms > self._mic_peak:
                        self._mic_peak = rms
                    else:
                        self._mic_peak *= _AUDIO_LEVEL_DECAY
                    denom = max(self._mic_peak, _AUDIO_LEVEL_MIN_PEAK_MIC)
                    self._latest_mic_level = min(1.0, rms / denom)
                else:
                    if rms > self._sys_peak:
                        self._sys_peak = rms
                    else:
                        self._sys_peak *= _AUDIO_LEVEL_DECAY
                    denom = max(self._sys_peak, _AUDIO_LEVEL_MIN_PEAK_SYS)
                    self._latest_sys_level = min(1.0, rms / denom)

    async def _audio_level_emitter(self) -> None:
        """Push per-source RMS amplitude to the HUD waveform at ~15 Hz.

        Gated by ``armed_event`` — when the agent is disarmed, no audio
        is flowing and there's nothing to report. Change-detected so a
        long silent stretch doesn't burn 15 pipe writes/sec on
        identical-to-three-decimals levels; the React side renders
        nothing new in that case anyway.
        """
        launcher = getattr(self.notifier, "launcher", None)
        if launcher is None:
            return
        prev_mic: float = -1.0
        prev_sys: float = -1.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self.arm.armed_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if self._stop.is_set():
                break
            mic = self._latest_mic_level
            sys_lvl = self._latest_sys_level
            if abs(mic - prev_mic) >= 0.005 or abs(sys_lvl - prev_sys) >= 0.005:
                try:
                    launcher.set_audio_levels(mic, sys_lvl)
                except Exception:
                    log.debug("[hud] set_audio_levels failed", exc_info=True)
                prev_mic = mic
                prev_sys = sys_lvl
            await asyncio.sleep(1.0 / 15.0)

    async def _prewarm_vads(self) -> None:
        """Load the Silero VAD model off the event loop, before the
        user arms for the first time.

        Defers cost away from the hot path so the first ``vad.feed()`` from
        ``_consume`` is a no-op fast path (``_model is not None`` check in
        ``vad.py::_ensure_loaded``). Without this, the first armed session
        eats an event-loop stall while silero-vad loads × 2 instances,
        and the asyncio loop falls far enough behind the producer
        callbacks (mic at 50 Hz, WASAPI loopback in 500 ms batches) that
        ``mic.queue`` / ``sys.queue`` (both ``maxsize=200`` ≈ 4 s) start
        rejecting frames as ``QueueFull``. The thread-pool path keeps
        the asyncio loop responsive during the load.

        Failure is **fatal**. A VAD that can't load means
        ``_consume``'s ``vad.feed()`` throws on every frame, both
        queues fill, every session captures 0 s of voiced time, and
        the cheap gate drops everything as ``gate_failed`` — the
        agent looks alive (heartbeat, hotkey, tray menu) while being
        silently useless. v3.0.0 shipped this way (faster-whisper was
        removed from pyproject.toml, taking ``onnxruntime`` with it as
        a transitive; silero-vad's onnx backend then failed to load).
        Crashing here surfaces the broken install instead of letting
        the user record empty sessions for hours.
        """
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self.vad_mic._ensure_loaded)
            await loop.run_in_executor(self._executor, self.vad_sys._ensure_loaded)
            log.info("[agent] VAD pre-warm complete (mic + system models loaded)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.critical(
                "[agent] VAD load failed — agent cannot capture. Likely "
                "a missing or broken silero-vad / torch install. "
                "Shutting down. (%s: %s)",
                type(exc).__name__, exc,
                exc_info=True,
            )
            self._stop.set()

    async def _ticker(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            self.detector.tick(now)
            self._maybe_heartbeat(now)
            self._maybe_run_upload_sweep(now)
            buffers = self.detector.take_closed_session()
            while buffers is not None:
                task = asyncio.create_task(self._process_session(buffers))
                self._processing_tasks.add(task)
                task.add_done_callback(self._processing_tasks.discard)
                buffers = self.detector.take_closed_session()

    def _maybe_heartbeat(self, now: float) -> None:
        """Emit a periodic status line so a user watching the terminal can
        confirm the agent is alive and see what state it's in. Disabled when
        cfg.heartbeat_secs <= 0."""
        interval = self.cfg.heartbeat_secs
        if interval <= 0:
            return
        if self._heartbeat_last == 0.0:
            self._heartbeat_last = now
            return
        if now - self._heartbeat_last < interval:
            return
        self._heartbeat_last = now

        d = self.detector
        kept = self._captures_kept
        discarded = self._captures_discarded
        echo_n = self._echo_segments_dropped
        echo_s = self._echo_secs_dropped
        arm_state = self.arm.state.value.upper()
        arm_reason = self.arm._reason
        reason_tag = ""
        if arm_reason is not None:
            label = arm_reason.app_key or arm_reason.source
            if arm_reason.target_pids:
                pids_csv = ",".join(str(p) for p in arm_reason.target_pids)
                scope_part = f"app[{pids_csv}]"
            else:
                scope_part = "endpoint"
            mic_part = arm_reason.mic_device or "default"
            # Truncate the device name so a long PortAudio identifier
            # ("Microphone (2- Realtek(R) Audio)") doesn't bloat the
            # heartbeat line.
            if len(mic_part) > 24:
                mic_part = mic_part[:23] + "…"
            reason_tag = f" ({label} scope={scope_part} mic={mic_part})"
        # HUD subprocess health, folded into the heartbeat so a user (or
        # triage) watching the terminal can tell at a glance whether the
        # notification layer is alive — without digging for [hud] lines.
        # This is the segment that would have made the multi-day "no toast"
        # incident a 30-second diagnosis. Best-effort: NoopNotifier (tests /
        # SAYZO_NOTIFICATIONS_ENABLED=0) has no launcher → omit the segment.
        hud_tag = ""
        _launcher = getattr(self.notifier, "launcher", None)
        if _launcher is not None:
            try:
                hd = _launcher.diagnose()
                flags = ["GIVEN_UP"] if hd.get("given_up") else []
                flags.append("alive" if hd.get("alive") else "DOWN")
                if hd.get("ready"):
                    flags.append("ready")
                rc = hd.get("respawn_count") or 0
                if rc:
                    flags.append(f"respawns={rc}")
                hud_tag = " hud=" + ",".join(flags)
            except Exception:
                hud_tag = " hud=?"
        if d.state == SessionState.OPEN and d._buffers is not None:
            elapsed = now - d._session_start_mono
            mic_voiced = d._buffers.mic_total_voiced()
            sys_voiced = d._buffers.sys_total_voiced()
            log.info(
                "[heartbeat] state=%s%s OPEN elapsed=%.1fs mic_voiced=%.1fs sys_voiced=%.1fs "
                "kept=%d discarded=%d echo_dropped=%d/%.0fs%s",
                arm_state, reason_tag,
                elapsed, mic_voiced, sys_voiced,
                kept, discarded, echo_n, echo_s,
                hud_tag,
            )
        elif d.state == SessionState.PENDING_CLOSE and d._buffers is not None:
            elapsed = now - d._session_start_mono
            last_any = max(d._last_voiced_mono["mic"], d._last_voiced_mono["system"])
            silence = now - last_any if last_any > 0 else 0.0
            log.info(
                "[heartbeat] state=%s%s PENDING_CLOSE elapsed=%.1fs silence=%.1fs "
                "kept=%d discarded=%d%s",
                arm_state, reason_tag, elapsed, silence,
                kept, discarded, hud_tag,
            )
        elif arm_state == "DISARMED":
            log.info(
                "[heartbeat] state=DISARMED waiting for hotkey or meeting detect "
                "kept=%d discarded=%d echo_dropped=%d/%.0fs%s",
                kept, discarded, echo_n, echo_s, hud_tag,
            )
        else:
            # ARMED but detector IDLE — should be a brief transient (between
            # disarm closing the session and the controller clearing the
            # armed flag). With session-on-arm, ARMED implies OPEN.
            log.info(
                "[heartbeat] state=%s%s IDLE (transient) "
                "kept=%d discarded=%d echo_dropped=%d/%.0fs%s",
                arm_state, reason_tag,
                kept, discarded, echo_n, echo_s, hud_tag,
            )

    def _maybe_run_upload_sweep(self, now: float) -> None:
        """Fire a retry sweep every `retry_sweep_interval_secs` monotonic seconds.
        Skipped while a previous sweep is still running so long sweeps can't
        stack. Set `cfg.upload.retry_sweep_interval_secs <= 0` to disable."""
        interval = self.cfg.upload.retry_sweep_interval_secs
        if interval <= 0:
            return
        if self._upload_sweep_last == 0.0:
            # Arm the timer; the startup_sweep background task covers the first run.
            self._upload_sweep_last = now
            return
        if now - self._upload_sweep_last < interval:
            return
        if self._sweep_in_progress:
            return
        self._upload_sweep_last = now
        self._sweep_in_progress = True
        task = asyncio.create_task(self._run_periodic_sweep())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_periodic_sweep(self) -> None:
        try:
            await self.retry_mgr.sweep_once()
        except Exception:
            log.warning("[upload] periodic sweep failed", exc_info=True)
        finally:
            self._sweep_in_progress = False

    async def _run_user_triggered_sweep(self) -> None:
        """User-clicked Try Again sweep. Clears any active credit pause
        first — the click means the user has topped up and wants a fresh
        server attempt. If credits are still out, the next 402 will re-arm
        the pause via the regular handler."""
        try:
            await self.retry_mgr.clear_credit_pause()
        except Exception:
            log.warning("[upload] clear_credit_pause failed", exc_info=True)
        await self._run_periodic_sweep()

    async def _process_session(self, buffers: SessionBuffers) -> None:
        """Public entry — registers an in-progress row for the Captures pane,
        then runs the actual pipeline. The proc_id is reused as the eventual
        record id so the in-progress row swaps cleanly to the on-disk row."""
        proc_id = uuid.uuid4().hex[:12]
        started_iso = (
            buffers.started_at.isoformat()
            if hasattr(buffers.started_at, "isoformat")
            else str(buffers.started_at)
        )
        self._processing_state[proc_id] = {
            "label": "Sayzo is analyzing this",
            "started_at": started_iso,
            "duration_secs": round(buffers.elapsed(), 1),
        }
        try:
            await self._process_session_inner(buffers, proc_id)
        finally:
            self._processing_state.pop(proc_id, None)

    async def _write_dropped_async(
        self,
        buffers: SessionBuffers,
        reason: str,
        proc_id: str,
        extra: dict | None = None,
    ) -> None:
        """Persist a tiny no-audio stub so the user can see this session was
        skipped in the Captures pane. Failures are logged, never raised."""
        loop = asyncio.get_running_loop()
        ended_at = buffers.started_at + timedelta(seconds=buffers.elapsed())
        try:
            await loop.run_in_executor(
                self._executor,
                lambda: self.sink.write_dropped(
                    buffers.started_at,
                    ended_at,
                    reason,
                    extra=extra,
                    rec_id=proc_id,
                ),
            )
        except Exception:
            log.warning(
                "[session] failed to write dropped stub (%s)", reason, exc_info=True
            )

    async def _process_session_inner(
        self, buffers: SessionBuffers, proc_id: str
    ) -> None:
        loop = asyncio.get_running_loop()
        sr = self.cfg.capture.sample_rate

        # 0a. AEC pre-pass — subtract speaker bleed from the mic at the
        # sample level using WebRTC AEC3 (livekit.rtc.apm). Default ON
        # since v3.6.1; SAYZO_AEC__ENABLED=0 to disable.
        #
        # v3.6.6: runs THREE sequential passes (the D-recipe from the
        # scripts/synth_double_talk_test.py prototype). Each pass feeds
        # the previous pass's cleaned mic back into AEC3 against the
        # ORIGINAL sys reference. The reference does NOT get re-cleaned
        # between passes — sys is the ground truth of what the speaker
        # played, and cleaning it would corrupt the echo model AEC3 is
        # trying to learn. Each pass uses a fresh APM instance, so the
        # adaptive filter re-converges on residual that survived the
        # previous pass; passes 2 and 3 attack the long-tail reverb that
        # pass 1's impulse-response window couldn't fully model.
        #
        # When AEC runs, the cleaned mic replaces buffers.mic_pcm so the
        # downstream echo_guard + DSP + windowing all consume the post-
        # AEC signal. echo_guard then acts as the non-linear residual
        # safety net (cheap-laptop speaker compression, BT codec re-
        # encoding) — see Critical design rule 5 in CLAUDE.md.
        aec_passes: list[aec.AecReport] = []
        aec_report = None
        if self.cfg.aec.enabled:
            cleaned_mic = bytes(buffers.mic_pcm)
            sys_pcm_bytes = bytes(buffers.sys_pcm)
            for i in range(3):
                cleaned_mic, rep = await loop.run_in_executor(
                    self._executor,
                    aec.cancel_echo,
                    cleaned_mic,
                    sys_pcm_bytes,
                    sr,
                    self.cfg.aec,
                )
                aec_passes.append(rep)
                if rep.ran:
                    log.info(
                        "[aec] pass %d/3 frames=%d dur=%.0fms lag=%+dsmp "
                        "peak=%.2f ns=%s hpf=%s mic_rms %.4f→%.4f sys_rms=%.4f",
                        i + 1,
                        rep.frames_processed, rep.duration_ms,
                        rep.lag_samples, rep.lag_xcorr_peak,
                        "on" if self.cfg.aec.noise_suppression else "off",
                        "on" if self.cfg.aec.high_pass_filter else "off",
                        rep.mic_rms_before, rep.mic_rms_after, rep.sys_rms,
                    )
                else:
                    # Silent buffers / livekit unavailable / APM error —
                    # cancel_echo returned input unchanged, so passes 2-3
                    # would do the same. Skip them.
                    log.info("[aec] pass %d/3 skipped (%s)", i + 1, rep.skip_reason)
                    break
            aec_report = aec_passes[-1]
            if aec_report.ran:
                buffers.mic_pcm = bytearray(cleaned_mic)

        # 0b. Echo guard — classify mic VAD segments as user speech or
        # speaker-to-mic bleed, then strip echo entries from
        # `buffers.mic_segments` and record them on `buffers.mic_echo_segments`.
        # Runs BEFORE the gate so passive "user listens to a podcast" sessions
        # fail substantive-user-turn on real (non-echo-inflated) mic totals,
        # avoiding upload of sessions where the user never actually spoke.
        # When AEC ran first (0a above), echo_guard operates on already-
        # linearly-cleaned mic — it's still the non-linear residual safety
        # net (cheap-speaker compression, BT codec re-encoding).
        eg_report = None
        if self.cfg.echo_guard.enabled:
            eg_report = await loop.run_in_executor(
                self._executor,
                echo_guard.classify_buffers,
                buffers, sr, self.cfg.echo_guard,
            )
            log.info(
                "[echo_guard] kept=%d dropped=%d dropped_secs=%.1f",
                eg_report.segments_kept,
                eg_report.segments_dropped,
                eg_report.seconds_dropped,
            )
            for r in eg_report.per_segment:
                lag_ms = int(round(r.lag_samples * 1000.0 / sr))
                if r.echo_spans:
                    # Already-dropped segments — one log line per echo span.
                    for es, ee in r.echo_spans:
                        log.info(
                            "[echo_guard]   drop %.2f-%.2fs coh=%.2f "
                            "resid_speech_p=%.2f lag=%dms xcorr=%.2f "
                            "rms_mic=%.3f rms_sys=%.3f reason=%s",
                            es, ee, r.coherence, r.residual_speech_prob,
                            lag_ms, r.xcorr_peak, r.mic_rms, r.sys_rms,
                            r.reason,
                        )
                else:
                    # Kept segments — log the scores too so we can see how
                    # close each segment came to the drop threshold. Critical
                    # observability for tuning thresholds AND for diagnosing
                    # "AEC working but echo audible in transcript" reports:
                    # with AEC decorrelating bleed from sys, echo_guard's
                    # coherence check can stop firing even when bleed is
                    # audible. We need to SEE the scores to know if a
                    # threshold tweak is warranted.
                    log.info(
                        "[echo_guard]   keep %.2f-%.2fs coh=%.2f "
                        "resid_speech_p=%.2f lag=%dms xcorr=%.2f "
                        "rms_mic=%.3f rms_sys=%.3f reason=%s",
                        r.original.start_ts, r.original.end_ts,
                        r.coherence, r.residual_speech_prob,
                        lag_ms, r.xcorr_peak, r.mic_rms, r.sys_rms,
                        r.reason,
                    )
            self._echo_segments_dropped += eg_report.segments_dropped
            self._echo_secs_dropped += eg_report.seconds_dropped

        # 1. Cheap gate (operates on echo-cleaned mic_segments)
        gate = evaluate_user_turn_gate(buffers, self.cfg.conversation)
        log.info("[gate] %s", gate.reason)
        if not gate.passed:
            log.info("[session] DISCARDED (failed cheap gate)")
            self._captures_discarded += 1
            await self._write_dropped_async(
                buffers,
                "gate_failed",
                proc_id,
                extra={
                    "gate_reason": gate.reason,
                    "mic_total": round(gate.mic_total, 2),
                },
            )
            try:
                self.notifier.notify(
                    "Capture discarded",
                    "Your speech was too brief to coach you on this one. Try a longer session.",
                )
            except Exception:
                log.debug("[app] discard toast failed", exc_info=True)
            return

        # 2. Apply DSP to the raw session PCM. Mic gets the full chain
        # (highpass + noisereduce + peak-norm); system gets a light touch
        # (highpass + peak-norm). Runs on the raw buffers because
        # noisereduce's stationary-mode noise estimator would collapse to
        # ~0 if it saw long stretches of synthetic zeros and effectively
        # disable itself; the per-channel windowing below removes echo /
        # dead-air regions from the encoded output instead.
        #
        # When AEC ran with NS=on this session, skip dsp.py's noisereduce
        # — APM's NS3 has already shaped the mic spectrum, and stacking
        # noisereduce on top re-estimates "noise" from an already-cleaned
        # signal, threshold drifts low, and you get phasey/musical-noise
        # artifacts (the same shape dsp.py:67-72 dialed prop_decrease
        # down to 0.5 to avoid in the first place). Per-session derived
        # config keeps the no-AEC path's denoise behavior intact.
        mic_dsp_cfg = self.cfg.capture
        if (
            aec_report is not None
            and aec_report.ran
            and self.cfg.aec.noise_suppression
        ):
            mic_dsp_cfg = self.cfg.capture.model_copy(
                update={"denoise_enabled": False}
            )
            log.info("[dsp] mic noisereduce skipped (APM NS3 already ran)")
        mic_dsp, sys_dsp = await asyncio.gather(
            loop.run_in_executor(
                self._executor, apply_mic_dsp,
                bytes(buffers.mic_pcm), sr, mic_dsp_cfg,
            ),
            loop.run_in_executor(
                self._executor, apply_sys_dsp,
                bytes(buffers.sys_pcm), sr, self.cfg.capture,
            ),
        )

        # 3. Slice the final audio at [first_speech - pad, last_speech + pad]
        # across both channels using identical sample indices. Mid-conversation
        # silences are preserved as recorded audio (thinking pauses, response
        # latency). Mic-only zeroing is then applied for any `mic_echo_segments`
        # spans inside the kept range — CLAUDE.md design rule 5 layered echo
        # defense. Identical indices on mic/sys are load-bearing for AEC
        # alignment (see memory `project_aec_misalignment_v3_6_0`).
        pad = self.cfg.conversation.final_audio_speech_pad_secs
        mic_final, sys_final, trim_report = apply_session_trim(
            mic_dsp,
            sys_dsp,
            buffers.mic_segments,
            buffers.sys_segments,
            buffers.mic_echo_segments,
            pad_secs=pad,
            sample_rate=sr,
        )
        log.info(
            "[sink] trimmed: %.1fs kept out of %.1fs total",
            trim_report.kept_secs,
            trim_report.original_secs,
        )

        # 4. Sink + upload
        # Derive wall-clock started_at / ended_at from the sliced PCM. Two
        # offsets shift `buffers.started_at`:
        #   - `backfill_secs`: session_t0_mono is earlier than started_monotonic
        #     when backfill extends the audio before _open_session was called.
        #   - `trim_report.start_offset_secs`: the post-DSP slice cut leading
        #     silence, so `mic_final[0]` is later than session_t0_mono.
        # `pcm_duration` comes from the sliced length, not `buffers.pcm_duration`
        # (which still reflects the un-sliced session buffer).
        backfill_secs = max(0.0, buffers.started_monotonic - buffers.session_t0_mono)
        true_started_at = (
            buffers.started_at
            - timedelta(seconds=backfill_secs)
            + timedelta(seconds=trim_report.start_offset_secs)
        )
        pcm_duration = len(mic_final) / 2 / sr
        ended_at = true_started_at + timedelta(seconds=pcm_duration)
        metadata = {
            "close_reason": buffers.close_reason.value if buffers.close_reason else None,
            "upload": empty_upload_state(),
            "trim": trim_report.as_metadata(),
        }
        if eg_report is not None:
            metadata["echo_guard"] = {
                "enabled": eg_report.enabled,
                "segments_kept": eg_report.segments_kept,
                "segments_dropped": eg_report.segments_dropped,
                "seconds_dropped": eg_report.seconds_dropped,
                "dropped_spans": [[s, e] for s, e in eg_report.dropped_spans],
                "thresholds": eg_report.thresholds,
            }
        if aec_passes:
            # Per-pass telemetry (v3.6.6). Most fields anchor to pass 1 so
            # mic_rms_before / sys_rms / lag still describe the ORIGINAL
            # signal, not pass N-1's output. mic_rms_after + frames_processed
            # come from the last pass — the final cleaned mic — and
            # duration_ms is the total CPU spent across all passes that ran.
            # mic_rms_after_per_pass lets us verify in the field that passes
            # 2 and 3 actually contributed (vs. AEC3 plateauing after pass 1).
            first = aec_passes[0]
            last = aec_passes[-1]
            ran_passes = [r for r in aec_passes if r.ran]
            metadata["aec"] = {
                "enabled": first.enabled,
                "ran": last.ran,
                "skip_reason": last.skip_reason,
                "lag_samples": first.lag_samples,
                "lag_xcorr_peak": round(first.lag_xcorr_peak, 4),
                "frames_processed": last.frames_processed,
                "duration_ms": round(
                    sum(r.duration_ms for r in aec_passes), 1
                ),
                "mic_rms_before": round(first.mic_rms_before, 4),
                "mic_rms_after": round(last.mic_rms_after, 4),
                "sys_rms": round(first.sys_rms, 4),
                "passes_run": len(ran_passes),
                "mic_rms_after_per_pass": [
                    round(r.mic_rms_after, 4) for r in ran_passes
                ],
            }
        record = await loop.run_in_executor(
            self._executor,
            lambda: self.sink.write(
                buffers.arm_app_key,
                true_started_at,
                ended_at,
                mic_final,
                sys_final,
                sr,
                metadata,
                rec_id=proc_id,
                arm_app_display=buffers.arm_app_display,
            ),
        )
        rec_dir = self.cfg.captures_dir / record.id
        # Live path bypasses the pause gate so a stale local credit/auth
        # block doesn't silently reject a brand-new capture. If credits are
        # genuinely exhausted, the server's 402 re-arms the pause for the
        # sweep; if the user already topped up, the upload just goes through.
        # ``live=True`` also opts this call into the "Conversation saved to Sayzo"
        # success toast — sweep-path retries (auto + manual Try Again) stay
        # silent to avoid a toast burst when a backlog drains.
        await self.retry_mgr.try_upload(
            record, rec_dir, bypass_pause_gate=True, live=True
        )
        self._captures_kept += 1

    # ---- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.cfg.ensure_dirs()

        # Drain any unuploaded captures from prior runs (failed transients,
        # stuck in_flight records, legacy records from before upload-state
        # tracking existed). Runs as a background task so capture can start
        # immediately even if the backlog is large.
        startup_sweep_task = asyncio.create_task(self.retry_mgr.startup_sweep())
        self._background_tasks.add(startup_sweep_task)
        startup_sweep_task.add_done_callback(self._background_tasks.discard)

        # Start the arm controller — registers the hotkey listener and
        # kicks off the whitelist watcher. Capture streams stay CLOSED
        # until the user arms via hotkey or accepts a consent toast.
        await self.arm.start()

        # First-launch welcome toast — fires once per install, flagged by
        # data_dir/welcomed.json so reopening the agent doesn't re-surface
        # the message.
        #
        # Note: the old tkinter onboarding walkthrough that used to run at
        # this point is gone. Everything it covered (hotkey, accessibility,
        # automation, per-permission explanations) now lives inside the
        # pywebview first-run window before the tray is even up.
        self._maybe_fire_welcome_toast()

        consumers = [
            asyncio.create_task(self._consume("mic", self.mic.queue, self.vad_mic)),
            asyncio.create_task(self._consume("system", self.sys.queue, self.vad_sys)),
            asyncio.create_task(self._ticker()),
            asyncio.create_task(self._audio_level_emitter()),
        ]
        # Pre-warm Silero VAD models in the background so the user's first
        # arm doesn't pay the synchronous load on the event loop. Without
        # this, the first frame fed to vad.feed() blocks _consume for the
        # load duration, mic.callback keeps pushing frames at 50 Hz,
        # mic.queue (maxsize=200 ≈ 4 s) overflows, and ~3 s of mic audio
        # gets dropped at session 1 start (see the 2026-05-14 logs).
        # Once-per-process; runs in a thread-pool executor so it can't
        # block the event loop. Also doubles as the startup health check
        # for the silero-vad / torch install — `_prewarm_vads` signals
        # `_stop` on failure so a broken bundle exits loudly instead of
        # capturing empty sessions forever (see its docstring).
        prewarm_task = asyncio.create_task(self._prewarm_vads())
        self._background_tasks.add(prewarm_task)
        prewarm_task.add_done_callback(self._background_tasks.discard)
        log.info(
            "[agent] running. Shortcut: %s. Ctrl+C to stop.",
            self.arm.current_hotkey,
        )
        try:
            await self._stop.wait()
        finally:
            for t in consumers:
                t.cancel()
            # Stop the arm controller — force-closes any open session,
            # stops streams, unregisters hotkey, cancels background watchers.
            await self.arm.stop()
            # Pick up any buffers force_close enqueued so they reach the sink.
            buffers = self.detector.take_closed_session()
            while buffers is not None:
                await self._process_session(buffers)
                buffers = self.detector.take_closed_session()
            # Wait for any in-flight _process_session tasks to finish
            if self._processing_tasks:
                log.info("[agent] waiting for %d in-flight session(s)...", len(self._processing_tasks))
                await asyncio.gather(*self._processing_tasks, return_exceptions=True)
            # Wait for background tasks (startup sweep, periodic sweep). Give
            # them a short grace window; if they're still running, cancel.
            if self._background_tasks:
                log.info("[agent] waiting for %d background task(s)...", len(self._background_tasks))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self._background_tasks, return_exceptions=True),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    for t in self._background_tasks:
                        t.cancel()
            self._executor.shutdown(wait=True)
            log.info("[agent] stopped")

    def _maybe_fire_welcome_toast(self) -> None:
        """Fire the first-launch welcome toast once per install.

        Flagged by ``data_dir/welcomed.json``. Non-interactive. Suppressed
        entirely when ``cfg.notifications_enabled`` or ``cfg.notify_welcome``
        is False.
        """
        if not self.cfg.notifications_enabled or not self.cfg.notify_welcome:
            return
        flag = self.cfg.data_dir / "welcomed.json"
        if flag.exists():
            return
        try:
            self.notifier.notify(
                "Sayzo is running",
                f"Press {self.arm.current_hotkey} anytime to start a meeting capture. "
                "We'll also ask you when we notice you're in a meeting.",
            )
        except Exception:
            log.debug("[agent] welcome toast failed (non-fatal)", exc_info=True)
        try:
            flag.write_text("{}", encoding="utf-8")
        except OSError:
            log.debug("[agent] welcome flag write failed (non-fatal)", exc_info=True)

    def pause(self) -> None:
        self._paused.set()
        log.info("[agent] paused")

    def resume(self) -> None:
        self._paused.clear()
        log.info("[agent] resumed")

    def stop(self) -> None:
        self._stop.set()
