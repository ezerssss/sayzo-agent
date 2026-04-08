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
from .capture.system import SystemCapture
from .config import Config
from .conversation import (
    ConversationDetector,
    SessionState,
    build_windowed_pcm,
    evaluate_user_turn_gate,
)
from .models import SessionBuffers, SpeechSegment, TranscriptLine
from .relevance import RelevanceLLM, RelevanceVerdict
from .sink import CaptureSink
from .speaker import SpeakerIdentifier
from .stt import WhisperSTT, TranscribedSegment
from .upload import NoopUploadClient, UploadClient
from .vad import SileroVAD

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, config: Config, upload_client: Optional[UploadClient] = None) -> None:
        self.cfg = config
        self.mic = MicCapture(
            sample_rate=config.capture.sample_rate,
            frame_ms=config.capture.frame_ms,
            device=config.capture.mic_device,
        )
        self.sys = SystemCapture(
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
        self.speaker = SpeakerIdentifier(config.speaker, voiceprint_path=config.voiceprint_path)
        self.llm = RelevanceLLM(config.llm, models_dir=config.models_dir)
        self.sink = CaptureSink(config.captures_dir)
        self.upload = upload_client or NoopUploadClient()

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eloquy-heavy")
        self._stop = asyncio.Event()
        self._heartbeat_last: float = 0.0
        self._captures_kept: int = 0
        self._captures_discarded: int = 0

    # ---- pipeline ----------------------------------------------------------

    async def _consume(self, source: str, queue: asyncio.Queue, vad: SileroVAD) -> None:
        while not self._stop.is_set():
            frame: np.ndarray = await queue.get()
            now = time.monotonic()
            self.detector.on_frame(source, frame, now)
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
                asyncio.create_task(self._process_session(buffers))
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

        # 2. Transcribe both sources in the heavy worker.
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
            self._executor, self._transcribe_both, bytes(buffers.mic_pcm), sys_pcm_for_stt, sr
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

        # 5. Sink + upload
        ended_at = buffers.started_at + timedelta(seconds=buffers.elapsed())
        record = await loop.run_in_executor(
            self._executor,
            self.sink.write,
            transcript,
            verdict.title,
            verdict.summary,
            verdict.relevant_span,
            buffers.started_at,
            ended_at,
            bytes(buffers.mic_pcm),
            bytes(buffers.sys_pcm),
            sr,
            {"close_reason": buffers.close_reason.value if buffers.close_reason else None},
        )
        await self.upload.upload(record)
        self._captures_kept += 1

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
        # Load voiceprint if present
        self.speaker.load_voiceprint()

        lines: list[TranscriptLine] = []

        # Mic segments → user (or "other" if voiceprint mismatch)
        for s in mic_segs:
            speaker_tag = "user"
            if self.speaker._voiceprint is not None:
                pcm_slice = self._slice_pcm_float(mic_pcm, s.start, s.end, sr)
                if pcm_slice.size > 0:
                    try:
                        emb = self.speaker.embed(pcm_slice)
                        if not self.speaker.is_user(emb):
                            speaker_tag = "other_unmic"
                    except Exception:
                        log.exception("speaker embed failed")
            lines.append(TranscriptLine(speaker=speaker_tag, start=s.start, end=s.end, text=s.text))

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
            # If user voice loops back through speakers, drop as duplicate
            if self.speaker._voiceprint is not None and self.speaker.is_user(emb):
                continue
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
            self._executor.shutdown(wait=True)
            log.info("[agent] stopped")

    def stop(self) -> None:
        self._stop.set()
