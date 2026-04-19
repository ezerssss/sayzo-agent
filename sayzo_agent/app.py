"""Async orchestrator wiring all pipeline stages."""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from .capture.mic import MicCapture
from .capture import SystemCapture
from .config import Config
from .conversation import (
    ConversationDetector,
    SessionState,
    build_windowed_pcm,
    evaluate_user_turn_gate,
    merge_close_segments,
)
from .models import SessionBuffers, SpeechSegment, TranscriptLine
from .relevance import RelevanceLLM, RelevanceVerdict
from .sink import CaptureSink
from .speaker import SpeakerIdentifier
from .stt import WhisperSTT, TranscribedSegment
from .notify import NoopNotifier, Notifier
from .upload import NoopUploadClient, UploadClient
from .vad import SileroVAD

log = logging.getLogger(__name__)


def _format_duration(secs: float) -> str:
    if secs >= 60:
        return f"{int(round(secs / 60))} min"
    return f"{int(round(secs))}s"


class Agent:
    def __init__(
        self,
        config: Config,
        upload_client: Optional[UploadClient] = None,
        mic_capture=None,
        sys_capture=None,
        notifier: Optional[Notifier] = None,
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
        self.stt = WhisperSTT(config.stt, models_dir=str(config.models_dir))
        self.speaker = SpeakerIdentifier(config.speaker)
        self.llm = RelevanceLLM(config.llm, models_dir=config.models_dir)
        self.sink = CaptureSink(config.captures_dir)
        self.upload = upload_client or NoopUploadClient()
        self.notifier: Notifier = notifier or NoopNotifier()

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sayzo-heavy")
        self._stop = asyncio.Event()
        self._paused = asyncio.Event()  # clear = running, set = paused
        self._processing_tasks: set[asyncio.Task] = set()
        self._heartbeat_last: float = 0.0
        self._captures_kept: int = 0
        self._captures_discarded: int = 0

    # ---- pipeline ----------------------------------------------------------

    async def _consume(self, source: str, queue: asyncio.Queue, vad: SileroVAD) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                await asyncio.sleep(0.5)
                continue
            # Queue payload is a (capture_mono_ts, frame) tuple. Capture-
            # stamping happens at the hardware boundary in each capture
            # module (sounddevice callback / WASAPI read / audio-tap
            # header); the detector uses `capture_mono_ts` to keep mic and
            # system buffers aligned to a shared session timeline.
            capture_mono_ts, frame = await queue.get()
            now = time.monotonic()
            self.detector.on_frame(source, frame, capture_mono_ts, now)
            for seg in vad.feed(frame):
                self.detector.on_segment(seg, now)

    async def _ticker(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()
            self.detector.tick(now)
            self.llm.maybe_unload(now)
            self._maybe_heartbeat(now)
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
        llm_state = "loaded" if d is not None and self.llm._llm is not None else "unloaded"
        kept = self._captures_kept
        discarded = self._captures_discarded
        if d.state == SessionState.OPEN and d._buffers is not None:
            elapsed = now - d._session_start_mono
            mic_voiced = d._buffers.mic_total_voiced()
            sys_voiced = d._buffers.sys_total_voiced()
            log.info(
                "[heartbeat] state=OPEN elapsed=%.1fs mic_voiced=%.1fs sys_voiced=%.1fs "
                "llm=%s kept=%d discarded=%d",
                elapsed,
                mic_voiced,
                sys_voiced,
                llm_state,
                kept,
                discarded,
            )
        else:
            mic_pre = len(d._pre_buffers["mic"]) / 2 / d.sample_rate
            sys_pre = len(d._pre_buffers["system"]) / 2 / d.sample_rate
            log.info(
                "[heartbeat] state=IDLE pre_buffer mic=%.1fs sys=%.1fs llm=%s "
                "kept=%d discarded=%d",
                mic_pre,
                sys_pre,
                llm_state,
                kept,
                discarded,
            )

    async def _process_session(self, buffers: SessionBuffers) -> None:
        # 1. Cheap gate
        gate = evaluate_user_turn_gate(buffers, self.cfg.conversation)
        log.info("[gate] %s", gate.reason)
        if not gate.passed:
            log.info("[session] DISCARDED (failed cheap gate)")
            self._captures_discarded += 1
            return

        loop = asyncio.get_running_loop()

        # 2a. Language probe on the mic stream. Sayzo is English-only, so if
        # the user was confidently speaking another language we bail now
        # rather than burn CPU transcribing nonsense (Whisper forced to
        # English on e.g. Tagalog produces hallucinated English). Set
        # STTConfig.non_english_discard_prob=1.0 to disable.
        mic_bytes = bytes(buffers.mic_pcm)
        if mic_bytes:
            mic_lang, mic_lang_prob = await loop.run_in_executor(
                self._executor, self.stt.detect_language, mic_bytes
            )
            log.info(
                "[stt] mic language probe: %s (prob=%.2f)", mic_lang, mic_lang_prob
            )
            if (
                mic_lang != "en"
                and mic_lang_prob >= self.cfg.stt.non_english_discard_prob
            ):
                log.info(
                    "[session] DISCARDED (mic confidently non-English: %s @ %.2f)",
                    mic_lang,
                    mic_lang_prob,
                )
                self._captures_discarded += 1
                return

        # 2b. Transcribe both sources in the heavy worker.
        # Density branch: when the user was barely present (e.g. passive media
        # + occasional comment), transcribe system audio only in ±pad windows
        # around mic VAD segments. Cuts STT cost dramatically without changing
        # discard logic — the LLM is still the source of truth.
        sr = self.cfg.capture.sample_rate
        elapsed = max(buffers.elapsed(), 1e-6)
        density = gate.mic_total / elapsed
        sys_pcm_full = bytes(buffers.sys_pcm)
        if density < self.cfg.conversation.stt_full_density:
            sys_pcm_for_stt = build_windowed_pcm(
                sys_pcm_full,
                buffers.mic_segments,
                pad_secs=self.cfg.conversation.stt_context_pad_secs,
                sample_rate=sr,
            )
            mode = "windowed"
        else:
            sys_pcm_for_stt = sys_pcm_full
            mode = "full"
        log.info(
            "[stt] mode=%s density=%.3f transcribing mic=%.1fs sys=%.1fs (orig sys=%.1fs)",
            mode,
            density,
            len(buffers.mic_pcm) / 2 / sr,
            len(sys_pcm_for_stt) / 2 / sr,
            len(sys_pcm_full) / 2 / sr,
        )
        mic_segs, sys_segs = await loop.run_in_executor(
            self._executor, self._transcribe_both, mic_bytes, sys_pcm_for_stt, sr
        )

        # 3. Speaker tagging + transcript merge
        transcript = await loop.run_in_executor(
            self._executor,
            self._build_transcript,
            mic_segs,
            sys_segs,
            bytes(buffers.mic_pcm),
            bytes(buffers.sys_pcm),
            sr,
        )
        for line in transcript:
            log.info("[transcript] %7.2fs %s: %s", line.start, line.speaker, line.text)

        if not transcript:
            log.info("[session] DISCARDED (empty transcript)")
            self._captures_discarded += 1
            return

        # 4. LLM relevance judgment
        total_duration = max(len(buffers.mic_pcm), len(buffers.sys_pcm)) / 2 / sr
        verdict: RelevanceVerdict = await loop.run_in_executor(
            self._executor, self.llm.judge, transcript, total_duration
        )
        log.info(
            "[llm] is_user_participant=%s is_real=%s span=%.1f→%.1f title=%r summary=%r",
            verdict.is_user_participant,
            verdict.is_real_conversation,
            verdict.relevant_span[0],
            verdict.relevant_span[1],
            verdict.title,
            verdict.summary[:120],
        )
        if not verdict.keep:
            log.info("[session] DISCARDED by LLM (reason=%s)", verdict.discard_reason)
            self._captures_discarded += 1
            return

        # 5. Trim dead air from the final audio. Zero-fill both channels
        # outside the union of mic + system VAD segments so the on-disk
        # capture doesn't carry minutes of hissing system audio during
        # silence, and Opus can compress the silent regions to near-zero
        # bits. Timestamps are preserved 1:1 — `relevant_span` and
        # transcript offsets still line up with the saved file.
        #
        # Before zeroing, we merge any two speech segments whose gap is
        # shorter than `final_audio_merge_gap_secs`. This preserves
        # conversational pauses (response latency, thinking beats, intra-
        # turn hesitation) as real audio — those pauses are coachable
        # signal for speech analysis. True dead air longer than the merge
        # gap still gets removed.
        raw_speech_segs = list(buffers.mic_segments) + list(buffers.sys_segments)
        speech_segs = merge_close_segments(
            raw_speech_segs, gap_secs=self.cfg.conversation.final_audio_merge_gap_secs
        )
        pad = self.cfg.conversation.final_audio_speech_pad_secs
        mic_final = build_windowed_pcm(
            bytes(buffers.mic_pcm), speech_segs, pad_secs=pad, sample_rate=sr
        )
        sys_final = build_windowed_pcm(
            bytes(buffers.sys_pcm), speech_segs, pad_secs=pad, sample_rate=sr
        )

        # Truncate trailing silence: cut both channels at the end of the
        # last speech segment + pad. The session buffer includes up to
        # joint_silence_close_secs of dead air at the tail — no reason to
        # keep it on disk.
        full_secs = len(buffers.mic_pcm) / 2 / sr
        if speech_segs:
            last_end = max(s.end_ts for s in speech_segs)
            cut_sample = min(int((last_end + pad) * sr), len(mic_final) // 2)
            cut_byte = cut_sample * 2
            mic_final = mic_final[:cut_byte]
            sys_final = sys_final[:cut_byte]

        kept_secs = len(mic_final) / 2 / sr
        log.info(
            "[sink] trimmed: %.1fs kept out of %.1fs total",
            kept_secs,
            full_secs,
        )

        # 6. Sink + upload
        # Derive wall-clock started_at / ended_at from the PCM timeline so
        # record.json lines up with the saved Opus file. `session_t0_mono`
        # may be earlier than `started_monotonic` (backfill extends the
        # audio earlier than the _open_session moment); `session_end_mono`
        # may be slightly later (tail pad equalizes channel lengths).
        backfill_secs = max(0.0, buffers.started_monotonic - buffers.session_t0_mono)
        true_started_at = buffers.started_at - timedelta(seconds=backfill_secs)
        pcm_duration = buffers.pcm_duration(sr)
        ended_at = true_started_at + timedelta(seconds=pcm_duration)
        record = await loop.run_in_executor(
            self._executor,
            self.sink.write,
            transcript,
            verdict.title,
            verdict.summary,
            verdict.relevant_span,
            true_started_at,
            ended_at,
            mic_final,
            sys_final,
            sr,
            {"close_reason": buffers.close_reason.value if buffers.close_reason else None},
        )
        await self.upload.upload(record)
        self._captures_kept += 1

        duration_s = (ended_at - true_started_at).total_seconds()
        body = f"{verdict.title} \u00b7 {_format_duration(duration_s)}"
        await loop.run_in_executor(
            self._executor, self.notifier.notify, "Conversation saved", body
        )

    def _transcribe_both(
        self, mic_pcm: bytes, sys_pcm: bytes, sr: int
    ) -> tuple[list[TranscribedSegment], list[TranscribedSegment]]:
        mic_segs = self.stt.transcribe_pcm16(mic_pcm, sample_rate=sr) if mic_pcm else []
        sys_segs = self.stt.transcribe_pcm16(sys_pcm, sample_rate=sr) if sys_pcm else []
        return mic_segs, sys_segs

    def _build_transcript(
        self,
        mic_segs: list[TranscribedSegment],
        sys_segs: list[TranscribedSegment],
        mic_pcm: bytes,
        sys_pcm: bytes,
        sr: int,
    ) -> list[TranscriptLine]:
        lines: list[TranscriptLine] = []

        # Mic segments → always "user". The mic is the user's own device;
        # whatever it picks up is treated as the user's speech.
        for s in mic_segs:
            lines.append(TranscriptLine(speaker="user", start=s.start, end=s.end, text=s.text))

        # System segments → other_N via greedy clustering on embeddings
        sys_embeds: list[np.ndarray] = []
        sys_keep_idx: list[int] = []
        for i, s in enumerate(sys_segs):
            pcm_slice = self._slice_pcm_float(sys_pcm, s.start, s.end, sr)
            if pcm_slice.size == 0:
                sys_embeds.append(np.zeros(256, dtype=np.float32))
                sys_keep_idx.append(i)
                continue
            try:
                emb = self.speaker.embed(pcm_slice)
            except Exception:
                emb = np.zeros(256, dtype=np.float32)
            sys_embeds.append(emb)
            sys_keep_idx.append(i)

        labels = self.speaker.cluster_others(sys_embeds) if sys_embeds else []
        for label, idx in zip(labels, sys_keep_idx):
            s = sys_segs[idx]
            lines.append(
                TranscriptLine(speaker=f"other_{label + 1}", start=s.start, end=s.end, text=s.text)
            )

        lines.sort(key=lambda l: l.start)
        return lines

    @staticmethod
    def _slice_pcm_float(pcm16: bytes, start_s: float, end_s: float, sr: int) -> np.ndarray:
        a = max(0, int(start_s * sr)) * 2
        b = max(a, int(end_s * sr)) * 2
        chunk = pcm16[a:b]
        if not chunk:
            return np.zeros(0, dtype=np.float32)
        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        # Resemblyzer chokes on near-silent slices (divide-by-zero in dBFS).
        # Treat anything below ~ -60 dBFS as empty so callers skip embedding.
        if arr.size == 0 or float(np.sqrt(np.mean(arr * arr))) < 1e-3:
            return np.zeros(0, dtype=np.float32)
        return arr

    # ---- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.cfg.ensure_dirs()
        await self.mic.start()
        await self.sys.start()

        consumers = [
            asyncio.create_task(self._consume("mic", self.mic.queue, self.vad_mic)),
            asyncio.create_task(self._consume("system", self.sys.queue, self.vad_sys)),
            asyncio.create_task(self._ticker()),
        ]
        log.info("[agent] running. Ctrl+C to stop.")
        try:
            await self._stop.wait()
        finally:
            for t in consumers:
                t.cancel()
            await self.mic.stop()
            await self.sys.stop()
            self.detector.force_close(time.monotonic())
            buffers = self.detector.take_closed_session()
            if buffers is not None:
                await self._process_session(buffers)
            # Wait for any in-flight _process_session tasks to finish
            if self._processing_tasks:
                log.info("[agent] waiting for %d in-flight session(s)...", len(self._processing_tasks))
                await asyncio.gather(*self._processing_tasks, return_exceptions=True)
            self._executor.shutdown(wait=True)
            log.info("[agent] stopped")

    def pause(self) -> None:
        self._paused.set()
        log.info("[agent] paused")

    def resume(self) -> None:
        self._paused.clear()
        log.info("[agent] resumed")

    def stop(self) -> None:
        self._stop.set()
