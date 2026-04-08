"""Local LLM relevance gate via llama-cpp-python.

Loaded lazily; unloaded after `idle_unload_secs` of inactivity to free RAM.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import LLMConfig
from .models import TranscriptLine

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a strict JSON classifier for the Eloquy English coaching platform.

You receive a transcript of a conversation captured from a user's machine. The transcript has speaker tags ("user" = the Eloquy user being coached; "other_1", "other_2", ... = other participants).

Your job: decide whether this conversation contains learnable data about the *user's* professional spoken English.

Return ONLY a JSON object with these exact fields:
{
  "is_user_participant": bool,   // true if the user actually participated (vs. background noise / passive listening)
  "is_real_conversation": bool,  // true if this is a real two-or-more-party conversation (not e.g. the user reading aloud, a YouTube video, music)
  "relevant_span": {"start_ts": float, "end_ts": float},  // generous span containing the user's substantive turns AND the surrounding context needed to understand them
  "title": string,               // short 3-7 word title for the conversation (e.g. "Demo of indexer pipeline", "Standup with backend team")
  "summary": string,             // 1-2 sentence neutral summary of the conversation
  "discard_reason": string|null  // null if keeping; short reason if discarding
}

Rules for relevant_span:
- Be VERY GENEROUS. Always include the other-side speech immediately BEFORE the user's first substantive turn (the question or prompt that the user was responding to) AND the other-side speech immediately AFTER (replies, follow-ups).
- When in doubt, prefer the entire transcript over a tight crop.
- Never crop out a question that the user answered.

Discard (set is_user_participant=false or is_real_conversation=false) if:
- The user only said filler ("yeah", "mhm", "thanks") with no substantive contribution.
- The "user" voice is actually a different person (background colleague, family member).
- The audio is a podcast/video/music with no real two-way exchange.

Output only the JSON object. No prose, no markdown fences.
"""


@dataclass
class RelevanceVerdict:
    is_user_participant: bool
    is_real_conversation: bool
    relevant_span: tuple[float, float]
    title: str
    summary: str
    discard_reason: Optional[str]

    @property
    def keep(self) -> bool:
        return self.is_user_participant and self.is_real_conversation and self.discard_reason is None


class RelevanceLLM:
    def __init__(self, cfg: LLMConfig, models_dir: Path) -> None:
        self.cfg = cfg
        self.models_dir = models_dir
        self._llm = None
        self._last_used: float = 0.0

    def _model_path(self) -> Path:
        return self.models_dir / self.cfg.filename

    def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        from llama_cpp import Llama  # lazy

        path = self._model_path()
        if not path.exists():
            raise FileNotFoundError(
                f"LLM weights not found at {path}. Run `eloquy-agent setup` first."
            )
        log.info("loading LLM %s (n_ctx=%d)", path.name, self.cfg.n_ctx)
        self._llm = Llama(
            model_path=str(path),
            n_ctx=self.cfg.n_ctx,
            n_threads=self.cfg.n_threads,
            verbose=False,
        )

    def maybe_unload(self, now: float) -> None:
        if self._llm is None:
            return
        if now - self._last_used >= self.cfg.idle_unload_secs:
            log.info("unloading LLM after %.0fs idle", now - self._last_used)
            self._llm = None

    def _format_transcript(self, lines: list[TranscriptLine]) -> str:
        out = []
        for ln in lines:
            out.append(f"[{ln.start:7.2f}-{ln.end:7.2f}] {ln.speaker}: {ln.text}")
        return "\n".join(out)

    def judge(self, lines: list[TranscriptLine], total_duration: float) -> RelevanceVerdict:
        self._ensure_loaded()
        assert self._llm is not None
        # NOTE: refresh _last_used at BOTH ends of this call. The start-side
        # refresh alone isn't enough — llama.cpp inference on a long session
        # can exceed idle_unload_secs, and without the trailing refresh the
        # ticker thread would wrongly decide the LLM was idle and unload it
        # as soon as judge() returns, forcing a pointless 2 GB reload on the
        # very next session. Keep both.
        self._last_used = time.monotonic()

        transcript_text = self._format_transcript(lines)
        user_prompt = (
            f"Total duration: {total_duration:.1f}s\n\n"
            f"Transcript:\n{transcript_text}\n\n"
            "Return the JSON verdict now."
        )

        resp = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        self._last_used = time.monotonic()
        raw = resp["choices"][0]["message"]["content"]
        return self._parse(raw, total_duration)

    def _parse(self, raw: str, total_duration: float) -> RelevanceVerdict:
        # Strip code fences if any
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        text = m.group(0) if m else raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM returned invalid JSON; defaulting to discard. raw=%r", raw[:300])
            return RelevanceVerdict(
                is_user_participant=False,
                is_real_conversation=False,
                relevant_span=(0.0, total_duration),
                title="",
                summary="",
                discard_reason="llm_invalid_json",
            )
        span = data.get("relevant_span") or {}
        start = float(span.get("start_ts", 0.0))
        end = float(span.get("end_ts", total_duration))
        # Pad the span by SPAN_PAD_SECS on each side as a safety net — small
        # local LLMs often crop too tight even when told not to. Clamped to
        # the actual session duration.
        SPAN_PAD_SECS = 15.0
        start = max(0.0, start - SPAN_PAD_SECS)
        end = min(total_duration, end + SPAN_PAD_SECS)
        return RelevanceVerdict(
            is_user_participant=bool(data.get("is_user_participant", False)),
            is_real_conversation=bool(data.get("is_real_conversation", False)),
            relevant_span=(start, end),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            discard_reason=data.get("discard_reason"),
        )
