"""Background polling for server-generated metadata after upload.

After an upload succeeds, the server returns immediately with a placeholder
title (synthetic timestamp) + empty summary. The real title/summary land on
``GET /api/captures/{id}`` once Deepgram + the quick-summary pass complete
(typically 5-30 s). Later still — once the server's analysis stage finishes —
the response carries a ``coaching_insight`` object (or null).

``CapturePoller`` fires a per-capture asyncio task with a backoff schedule and
caches ``title`` / ``summary`` into local ``record.json`` as soon as the server
reports a post-transcription status.

Two modes, selected by ``owns_toast`` (set by ``UploadRetryManager`` —
feature on AND (live capture OR the capture is still fresh, i.e. ended
within ``INSIGHT_FRESHNESS_GATE_SECS``); the freshness clause is what lets
a capture whose live upload failed and got swept moments later still show
the card — see ``upload_retry`` + [[project_mac_no_insight_card]]):

* ``owns_toast=False`` (stale backlog re-uploads, or the post-capture feedback
  feature disabled): legacy behavior — cache title/summary, then stop on the
  first cached title or a terminal status. No toast.
* ``owns_toast=True``: keep polling until ``status == "analyzed"`` (the only
  point the server populates/trusts ``coaching_insight`` — server-confirmed),
  a terminal failure, or the schedule is exhausted. Then fire ONE compact
  coaching-insight card (see ``gui/hud`` InsightCard), or a fallback "Capture
  saved" toast when no insight is produced. This replaces the immediate saved
  toast that ``upload_retry`` suppresses when the feature is on.

No persistence across agent restarts — if the agent dies mid-poll, the local
title stays as the synthetic placeholder and any pending insight toast is lost;
the webapp has its own polling and renders the same insight as a hero card on
the deep-link target. Fire-and-forget by design.
"""
from __future__ import annotations

import asyncio
import logging
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from .auth.exceptions import AuthenticationRequired, AuthTemporarilyUnavailable
from .sink import local_clock_label, read_record_from_dir, write_record_atomic
from .upload import parse_json_body

if TYPE_CHECKING:
    from .config import Config
    from .models import ConversationRecord
    from .notify import Notifier

log = logging.getLogger(__name__)


# Seconds-between-checks for ``GET /api/captures/{id}``. Dense early ticks
# catch title/summary fast (lands 5-30 s after upload — keeps Settings →
# Captures readable promptly); the multi-minute tail covers the much
# slower ``analyzed`` step. Server estimates (updated 2026-05-28 after
# the deep-analysis model swapped from gpt-4o-mini to gpt-5 reasoning to
# fix generic-insight outputs): p50 ≈ 5-8 min, p95 ≈ 10-15 min, worst case
# ≈ 20+ min. Total schedule reach ≈ 28 min (10+30+60+120+240+300×4) —
# above the server's "push give-up to ~20 min" floor with ~8 min headroom
# past their stated worst case. Past ~15 min the server team calls a
# capture "probably stuck" (deploy gap / OpenAI outage / genuine failure),
# so don't extend the cap speculatively. Ticks 3-4 (100s, 220s) are now
# structurally before any plausible ``analyzed`` under gpt-5 — kept as
# redundant title/summary catches rather than restructured (1-2 extra GETs
# per capture is cheap). Defer-cap interaction: ``_INSIGHT_DEFER_MAX_SECS``
# (1 h) is measured from insight-ready, so end-to-end wait can hit ~80 min
# before drop under gpt-5; ``_freshness_label`` renders honestly off
# ``record.ended_at``. RE-TUNE this block when the server ships
# ``analyzedAt`` and we have real percentiles (the gpt-5 numbers above are
# also structural estimates). Non-owning polls stop on the first cached
# title, so the tail only applies to ``owns_toast`` captures.
DEFAULT_POLL_SCHEDULE: tuple[float, ...] = (
    10.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0, 300.0,
)

# Status values past transcription where the server's ``title`` is the
# real generated one. Set on the first response that lands in this bucket.
PROGRESSED_STATUSES = frozenset({
    "transcribed",
    "validating",
    "validated",
    "analyzing",
    "profiling",
    "analyzed",
})

# How long the insight card dwells before auto-expiring. Long enough to read
# a short card and act, short enough that it doesn't linger as clutter (it
# also carries a dismiss X). Tunable; not a user-facing setting.
_INSIGHT_TOAST_TTL_SECS = 120.0

# If the user is in ANOTHER meeting (armed) when an insight is ready, hold the
# toast and fire on the next disarm rather than interrupting them. Drop it
# after this many seconds of waiting — a stale insight fired hours later
# defeats the "while it's fresh" pitch.
_INSIGHT_DEFER_MAX_SECS = 3600.0
_DEFER_POLL_SECS = 10.0


# Freshness buckets shown in the insight card's chip. Computed at fire time
# from ``record.ended_at`` so a deferred fire (user in another meeting when
# the insight became ready) doesn't claim "Just now" for a 50-minute-old
# capture. Bucket boundaries are loose — the chip is glanceable copy, not a
# stopwatch — and they're capped at hours because the defer staleness cap
# is 1 h so >1 h labels are vanishingly rare in practice.
def _freshness_label(ended_at: datetime | None, now: datetime | None = None) -> str:
    if ended_at is None:
        return "Just now"
    try:
        ref = now or datetime.now(timezone.utc)
        elapsed = (ref - ended_at).total_seconds()
    except Exception:
        return "Just now"
    if elapsed < 90:
        return "Just now"
    minutes = int(elapsed // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return "1 hr ago" if hours == 1 else f"{hours} hr ago"


# Terminal statuses — stop polling regardless of whether we cached anything.
# ``analyzed`` is the happy terminal; ``rejected`` and any ``*_failed`` mean
# the server gave up on the capture and the title won't change further.
def _is_terminal(status: str) -> bool:
    return status == "analyzed" or status == "rejected" or status.endswith("_failed")


class CapturePoller:
    """Polls the server for late-arriving title/summary/insight and acts on them.

    Wired from ``Agent`` and called by ``UploadRetryManager`` on
    ``UploadOutcome.SUCCESS``. The auth client may be ``None`` (e.g.
    ``NoopUploadClient`` path); in that case ``poll`` is a no-op.

    ``notifier`` / ``config`` / ``armed_check`` are only needed for the
    ``owns_toast`` path (firing the post-capture coaching card). They default
    to ``None`` so unit tests of the title/summary caching path don't need to
    wire them.
    """

    def __init__(
        self,
        auth_client,
        captures_dir: Path,
        executor: ThreadPoolExecutor,
        clock: Callable[[], datetime] | None = None,
        schedule: tuple[float, ...] = DEFAULT_POLL_SCHEDULE,
        *,
        notifier: "Notifier | None" = None,
        config: "Config | None" = None,
        armed_check: Callable[[], bool] | None = None,
    ) -> None:
        self._auth_client = auth_client
        self._captures_dir = captures_dir
        self._executor = executor
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._schedule = schedule
        self._notifier = notifier
        self._config = config
        self._armed_check = armed_check

    async def poll(
        self,
        rec_dir: Path,
        server_capture_id: str,
        owns_toast: bool = False,
    ) -> None:
        """Run the poll schedule for one capture.

        ``owns_toast=False`` (default): cache title/summary, stop on the first
        cached title or a terminal status — the legacy behavior. No toast.

        ``owns_toast=True``: keep polling to ``analyzed`` (or terminal failure
        / schedule exhaustion), then fire the post-capture coaching card or a
        feedback-ready toast when no insight was produced. Silent on errors —
        polling failures leave the local placeholder in place; the webapp shows
        live state on click.
        """
        if self._auth_client is None:
            return

        cached = False
        insight: dict | None = None
        for delay in self._schedule:
            await asyncio.sleep(delay)
            try:
                body = await self._fetch(server_capture_id)
            except AuthTemporarilyUnavailable:
                # Transient network blip (cold-boot race) — keep polling on the
                # schedule rather than abandoning the title/insight fetch. Must
                # precede the AuthenticationRequired clause (it's a subclass).
                log.debug(
                    "[poller] auth server unreachable for id=%s — will retry",
                    server_capture_id,
                )
                continue
            except AuthenticationRequired:
                log.debug(
                    "[poller] auth required for id=%s — giving up",
                    server_capture_id,
                )
                return
            except Exception:
                log.debug(
                    "[poller] fetch failed id=%s (will retry on next tick)",
                    server_capture_id, exc_info=True,
                )
                continue
            if body is None:
                continue

            status = str(body.get("status") or "")
            if not cached and status in PROGRESSED_STATUSES:
                try:
                    applied = await self._apply_server_metadata(rec_dir, body)
                except Exception:
                    log.debug(
                        "[poller] apply failed id=%s (non-fatal)",
                        server_capture_id, exc_info=True,
                    )
                    applied = False
                if applied:
                    cached = True
                    log.info(
                        "[poller] cached server metadata id=%s status=%s",
                        server_capture_id, status,
                    )

            if owns_toast:
                # The insight only exists at ``analyzed`` (server contract:
                # ``coaching_insight`` is null until then). Extract + persist,
                # then stop. Title/summary were cached just above this tick.
                if status == "analyzed":
                    insight = self._extract_insight(body)
                    await self._persist_insight(rec_dir, insight)
                    log.info(
                        "[poller] analyzed id=%s has_insight=%s",
                        server_capture_id, insight is not None,
                    )
                    break
                # Keep polling toward ``analyzed``. Only a terminal FAILURE
                # ends the wait early (nothing more will come).
                if status == "rejected" or status.endswith("_failed"):
                    log.info(
                        "[poller] terminal-without-insight id=%s status=%s",
                        server_capture_id, status,
                    )
                    break
            else:
                # Legacy: stop once title is cached or status is terminal —
                # title/summary won't change further, so any remaining ticks
                # are wasted GETs.
                if cached or _is_terminal(status):
                    log.debug(
                        "[poller] stopping id=%s status=%s cached=%s",
                        server_capture_id, status, cached,
                    )
                    return

        if not owns_toast:
            if not cached:
                log.debug(
                    "[poller] schedule exhausted id=%s without progressed status",
                    server_capture_id,
                )
            return

        # owns_toast: fire the insight card, or a feedback-ready toast when no
        # insight was produced (analyzed-with-null, terminal failure, or the
        # schedule exhausted before analysis finished).
        await self._fire_post_capture(rec_dir, server_capture_id, insight)

    async def _fetch(self, server_capture_id: str) -> dict | None:
        """One GET /api/captures/{id}. Returns the parsed JSON body or None.

        Treats any non-2xx as "not ready" — never raises. The poller is a
        best-effort fire-and-forget background task; transient server errors
        just mean we try again on the next tick.
        """
        resp = await self._auth_client.get(
            f"/api/captures/{server_capture_id}",
            timeout=httpx.Timeout(15.0),
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        return parse_json_body(resp)

    async def _apply_server_metadata(self, rec_dir: Path, body: dict) -> bool:
        """Overwrite local title / summary from the polled response.

        Each field is independent — we write whichever ones the server
        supplied, leave the rest as the client placeholder. Non-string
        values are ignored.

        Returns True if record.json was rewritten, False otherwise.
        """
        title = body.get("title")
        summary = body.get("summary")

        new_title: str | None = None
        if isinstance(title, str) and title.strip():
            new_title = title

        new_summary: str | None = None
        if isinstance(summary, str):
            new_summary = summary

        if new_title is None and new_summary is None:
            return False

        loop = asyncio.get_running_loop()

        def _do() -> bool:
            record = read_record_from_dir(rec_dir)
            changed = False
            if new_title is not None and record.title != new_title:
                record.title = new_title
                changed = True
            if new_summary is not None and record.summary != new_summary:
                record.summary = new_summary
                changed = True
            if changed:
                write_record_atomic(rec_dir, record)
            return changed

        return await loop.run_in_executor(self._executor, _do)

    # ------------------------------------------------------------------
    # Post-capture coaching insight (owns_toast path)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_insight(body: dict) -> dict | None:
        """Validate + clean the server's ``coaching_insight`` object.

        Returns a dict with required ``headline`` + ``body`` (and optional
        ``quote`` / ``type`` / ``why``), or ``None`` when the server returned
        null / a malformed object / missing the required fields. "No insight"
        is a first-class outcome (server contract) — the caller fires a
        feedback-ready toast in that case.
        """
        raw = body.get("coaching_insight")
        if not isinstance(raw, dict):
            return None
        headline = raw.get("headline")
        body_text = raw.get("body")
        if not (isinstance(headline, str) and headline.strip()):
            return None
        if not (isinstance(body_text, str) and body_text.strip()):
            return None
        insight: dict[str, str] = {
            "headline": headline.strip(),
            "body": body_text.strip(),
        }
        quote = raw.get("quote")
        if isinstance(quote, str) and quote.strip():
            insight["quote"] = quote.strip()
        itype = raw.get("type")
        if isinstance(itype, str) and itype.strip():
            insight["type"] = itype.strip()
        why = raw.get("why")
        if isinstance(why, str) and why.strip():
            insight["why"] = why.strip()
        return insight

    async def _persist_insight(self, rec_dir: Path, insight: dict | None) -> None:
        """Write the validated insight to ``record.json::metadata.coaching_insight``.

        Durable + debuggable; the Captures pane / future UI can read it back.
        No-op when there's no insight.
        """
        if not insight:
            return
        loop = asyncio.get_running_loop()

        def _do() -> None:
            record = read_record_from_dir(rec_dir)
            record.metadata["coaching_insight"] = insight
            write_record_atomic(rec_dir, record)

        try:
            await loop.run_in_executor(self._executor, _do)
        except Exception:
            log.debug("[poller] persist insight failed", exc_info=True)

    async def _fire_post_capture(
        self,
        rec_dir: Path,
        server_capture_id: str,
        insight: dict | None,
    ) -> None:
        """Fire the insight card (or no-insight feedback-ready toast), respecting live gates.

        Re-checks the master + feedback flags live (the user may have toggled
        during the minutes-long poll) and defers the fire if they're currently
        in another meeting.
        """
        cfg = self._config
        if cfg is None or self._notifier is None:
            return
        # Live re-check — these may have flipped during the poll.
        if not bool(getattr(cfg, "notifications_enabled", True)):
            return
        if not bool(getattr(cfg, "notify_capture_feedback", False)):
            # Feature turned off mid-poll. The immediate saved toast was
            # already suppressed at upload time, so we simply stay silent —
            # the capture is still saved + visible in the Captures pane.
            return

        auth = getattr(cfg, "auth", None)
        base = (getattr(auth, "effective_server_url", "") or "").rstrip("/")
        if not base:
            return  # No webapp base → no deep-link → nothing useful to show.
        deep_link = f"{base}/app/conversations/{server_capture_id}"

        if insight:
            fire_fn = self._make_insight_fire(rec_dir, deep_link, insight)
        else:
            fire_fn = self._make_no_insight_fire(rec_dir, deep_link)
        await self._fire_or_defer(fire_fn)

    def _make_insight_fire(
        self, rec_dir: Path, deep_link: str, insight: dict,
    ) -> Callable[[], None]:
        # Read the record once for both source_label and ended_at —
        # pre-refactor this method called _source_label which read the
        # record again, doubling the I/O when the path also persisted
        # the insight earlier in the call chain.
        try:
            record = read_record_from_dir(rec_dir)
        except Exception:
            record = None
        source_label = self._source_label(record)
        ended_at = record.ended_at if record is not None else None

        def _open() -> None:
            try:
                webbrowser.open(deep_link)
            except Exception:
                log.debug("[poller] webbrowser.open failed for %r", deep_link, exc_info=True)

        def fire() -> None:
            # Freshness is computed HERE, inside the closure body, not at
            # factory-build time — the closure runs after the defer-wait
            # in _fire_or_defer, which can add up to 1 h on top of the
            # ~28 min poll schedule. Computing at fire time keeps the
            # chip honest under deferred fires.
            freshness = _freshness_label(ended_at)
            try:
                self._notifier.notify_insight(  # type: ignore[union-attr]
                    headline=insight["headline"],
                    body=insight["body"],
                    source_label=source_label,
                    freshness_label=freshness,
                    quote=insight.get("quote"),
                    insight_type=insight.get("type"),
                    button_label="See full feedback",
                    on_pressed=_open,
                    expire_after_secs=_INSIGHT_TOAST_TTL_SECS,
                    secondary_button_label="Stop showing these",
                    on_secondary_pressed=self._disable_feedback,
                )
            except Exception:
                log.debug("[poller] notify_insight failed", exc_info=True)

        return fire

    def _make_no_insight_fire(
        self, rec_dir: Path, deep_link: str,
    ) -> Callable[[], None]:
        """Feedback-ready toast for the no-insight case (analyzed-with-null,
        terminal failure, or schedule exhaustion).

        The server produced no single coaching highlight, but the conversation
        page still has the transcript, replay-to-practice, and per-utterance
        coaching moments — so this actively invites the user to review it
        (personalized to the call, mirroring the insight card's source anchor)
        rather than reading as a passive "saved" confirmation, preserving the
        click-through we'd otherwise lose when there's no insight card.

        Distinct from the immediate "Conversation saved to Sayzo" toast
        ``upload_retry`` fires when the feedback feature is OFF: that one is a
        bare save confirmation for a user who opted out of coaching; this one
        drives the click-through. The two deliberately diverge in copy + intent.
        """
        try:
            record = read_record_from_dir(rec_dir)
        except Exception:
            record = None
        source_label = self._source_label(record)

        def _open() -> None:
            try:
                webbrowser.open(deep_link)
            except Exception:
                log.debug("[poller] webbrowser.open failed for %r", deep_link, exc_info=True)

        def fire() -> None:
            try:
                self._notifier.notify_actionable(  # type: ignore[union-attr]
                    f"Your {source_label} is ready to review",
                    "Replay it and see your coaching moments.",
                    button_label="See feedback",
                    on_pressed=_open,
                    expire_after_secs=15.0,
                )
            except Exception:
                log.debug("[poller] no-insight toast failed", exc_info=True)

        return fire

    @staticmethod
    def _source_label(record: "ConversationRecord | None") -> str:
        """Short "from your ___" anchor for the insight card's chip.

        Always derives from agent-side metadata, NEVER from ``record.title``:
        the chip is the "this is from a meeting you just had" recognition
        cue, and time + source is a stronger hit than the server's topical
        title ("Q4 planning sync") because freshness + source bind to a
        lived moment without requiring the user to read + match. The
        topical title still drives Settings → Captures + the deep-link
        hero card — those are about subject recognition; this isn't.

        Bonus: the chip's wording is now deterministic regardless of
        whether the server's title pass succeeded.

        Fallback chain:
          * ``metadata.arm_app_display`` + " call"   ("Microsoft Teams call")
          * ``metadata.arm_app_key.title()`` + " call"   ("Discord call")
          * "conversation"   (hotkey arms with no app attribution)

        Combined with the cached ``metadata.local_clock_label`` ("2:30 pm")
        the chip reads: "from your 2:30 pm Zoom call" /
        "from your 2:30 pm conversation".
        """
        if record is None:
            return "conversation"
        meta = record.metadata or {}
        clock = (meta.get("local_clock_label") or "").strip()
        if not clock and record.started_at is not None:
            # Legacy records (pre-cache) — recompute now, accepting the
            # small TZ-drift risk for the user-traveled case. New records
            # always carry the cached label so this is the cold-start path
            # for one-time backfill.
            clock = local_clock_label(record.started_at)
        display = (meta.get("arm_app_display") or "").strip()
        key = (meta.get("arm_app_key") or "").strip()
        if display:
            source = f"{display} call"
        elif key:
            source = f"{key.title()} call"
        else:
            source = "conversation"
        return f"{clock} {source}" if clock else source

    def _disable_feedback(self) -> None:
        """Off-switch behind the card's "Stop showing these" button.

        Runs in this (live agent) process, so it flips the in-process cfg and
        persists to user_settings.json directly — no IPC needed. A Settings
        window open at the time picks the new value up on its next open.
        """
        cfg = self._config
        if cfg is not None:
            try:
                cfg.notify_capture_feedback = False  # type: ignore[attr-defined]
            except Exception:
                log.debug("[poller] cfg.notify_capture_feedback mutation failed", exc_info=True)
            try:
                from . import settings_store
                settings_store.save(cfg.data_dir, {"notify_capture_feedback": False})
            except Exception:
                log.debug(
                    "[poller] persist notify_capture_feedback=False failed", exc_info=True
                )
        if self._notifier is not None:
            try:
                self._notifier.notify(
                    "Okay, no more insights",
                    "Re-enable anytime in Settings → Notifications.",
                )
            except Exception:
                log.debug("[poller] stop-showing confirmation toast failed", exc_info=True)

    async def _fire_or_defer(self, fire_fn: Callable[[], None]) -> None:
        """Fire now if disarmed; else hold until the next disarm (bounded).

        Firing capture #1's insight in the middle of capture #2 would be bad
        UX. ``armed_check`` reflects live arm state; we poll it and fire once
        it clears, dropping the toast after the staleness cap.
        """
        if self._armed_check is not None:
            waited = 0.0
            while self._is_armed():
                if waited >= _INSIGHT_DEFER_MAX_SECS:
                    log.info(
                        "[poller] dropping post-capture toast — still armed after %.0fs",
                        _INSIGHT_DEFER_MAX_SECS,
                    )
                    return
                await asyncio.sleep(_DEFER_POLL_SECS)
                waited += _DEFER_POLL_SECS
        try:
            fire_fn()
        except Exception:
            log.debug("[poller] deferred fire raised", exc_info=True)

    def _is_armed(self) -> bool:
        try:
            return bool(self._armed_check and self._armed_check())
        except Exception:
            return False
