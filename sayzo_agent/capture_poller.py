"""Background polling for server-generated metadata after upload.

After an upload succeeds, the server returns immediately with a placeholder
title (synthetic timestamp) + empty summary. The real title/summary land on
``GET /api/captures/{id}`` once Deepgram + the quick-summary pass complete
(typically 5-30 s). Later still — once the server's analysis stage finishes —
the response carries a ``coaching_insight`` object (or null).

``CapturePoller`` fires a per-capture asyncio task with a backoff schedule and
caches ``title`` / ``summary`` into local ``record.json`` as soon as the server
reports a post-transcription status.

Two modes, selected by ``owns_toast`` (set by ``UploadRetryManager`` to
``live and notify_capture_feedback`` at upload time):

* ``owns_toast=False`` (sweep re-uploads, or the post-capture feedback feature
  disabled): legacy behavior — cache title/summary, then stop on the first
  cached title or a terminal status. No toast.
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

from .auth.exceptions import AuthenticationRequired
from .sink import read_record_from_dir, write_record_atomic
from .upload import parse_json_body

if TYPE_CHECKING:
    from .config import Config
    from .notify import Notifier

log = logging.getLogger(__name__)


# Seconds-between-checks for ``GET /api/captures/{id}``. Dense early ticks
# catch the common case fast (server estimate: p50 ≈ 2-3 min to ``analyzed``)
# so the insight toast lands while the meeting is fresh; the ~5-min backoff
# tail covers the slow tail (p95 ≈ 4-6 min, but bursty concurrency / long
# captures / a server deploy gap can push past ~8 min). Total reach ≈ 28 min
# (10+30+60+120+240+300×4). RE-TUNE this tail once the server ships
# ``analyzedAt`` and we have real percentiles. Non-owning polls stop on the
# first cached title, so the tail only applies to ``owns_toast`` captures.
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
        fallback saved toast. Silent on errors — polling failures leave the
        local placeholder in place; the webapp shows live state on click.
        """
        if self._auth_client is None:
            return

        cached = False
        insight: dict | None = None
        for delay in self._schedule:
            await asyncio.sleep(delay)
            try:
                body = await self._fetch(server_capture_id)
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

        # owns_toast: fire the insight card, or a fallback saved toast when no
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
        fallback saved toast in that case.
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
        """Fire the insight card (or fallback saved toast), respecting live gates.

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
            fire_fn = self._make_fallback_saved_fire(deep_link)
        await self._fire_or_defer(fire_fn)

    def _make_insight_fire(
        self, rec_dir: Path, deep_link: str, insight: dict,
    ) -> Callable[[], None]:
        source_label = self._source_label(rec_dir)

        def _open() -> None:
            try:
                webbrowser.open(deep_link)
            except Exception:
                log.debug("[poller] webbrowser.open failed for %r", deep_link, exc_info=True)

        def fire() -> None:
            try:
                self._notifier.notify_insight(  # type: ignore[union-attr]
                    headline=insight["headline"],
                    body=insight["body"],
                    source_label=source_label,
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

    def _make_fallback_saved_fire(self, deep_link: str) -> Callable[[], None]:
        """Fallback "Capture saved" toast when no insight was produced.

        Same shape as the immediate saved toast ``upload_retry`` fires when the
        feedback feature is OFF — preserves upload confirmation under the
        "replace, don't stack" model.
        """
        def _open() -> None:
            try:
                webbrowser.open(deep_link)
            except Exception:
                log.debug("[poller] webbrowser.open failed for %r", deep_link, exc_info=True)

        def fire() -> None:
            try:
                self._notifier.notify_actionable(  # type: ignore[union-attr]
                    "Capture saved to Sayzo",
                    "Open it to see your transcript and drills.",
                    button_label="Open in Sayzo",
                    on_pressed=_open,
                    expire_after_secs=30.0,
                )
            except Exception:
                log.debug("[poller] fallback saved-toast failed", exc_info=True)

        return fire

    def _source_label(self, rec_dir: Path) -> str:
        """Short "from your ___" anchor, derived from the local record title.

        Strips the " · timestamp" suffix off placeholder titles ("Zoom call ·
        2026-…" → "Zoom call") and uses a real server title verbatim ("Q4
        planning sync"). Falls back to a generic label.
        """
        try:
            record = read_record_from_dir(rec_dir)
            title = (record.title or "").strip()
        except Exception:
            title = ""
        short = title.split(" · ")[0].strip() if title else ""
        return short or "recent meeting"

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
