"""Background polling for server-generated title/summary after upload.

After an upload succeeds, the server returns immediately with a placeholder
title (synthetic timestamp) + empty summary. The real title/summary land on
``GET /api/captures/{id}`` once Deepgram + the quick-summary pass complete
(typically 5-30 s).

``CapturePoller`` fires a per-capture asyncio task with a sparse schedule
(10s, 30s, 60s, 120s, 240s after upload — 5 checks over 4 min) and caches
``title`` / ``summary`` into local ``record.json`` once the server reports a
post-transcription status. Terminates on terminal status (``analyzed`` /
``rejected`` / ``*_failed``) or when the schedule is exhausted.

No persistence across agent restarts — if the agent dies mid-poll, the
local title stays as the synthetic placeholder; the webapp has its own
polling and shows the real title there. Fire-and-forget by design.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from .auth.exceptions import AuthenticationRequired
from .sink import read_record_from_dir, write_record_atomic
from .upload import parse_json_body

log = logging.getLogger(__name__)


# Seconds after upload to check ``GET /api/captures/{id}``. Sparse so we
# don't hammer the server while quick-summary is still running.
DEFAULT_POLL_SCHEDULE: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0, 240.0)

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

# Terminal statuses — stop polling regardless of whether we cached anything.
# ``analyzed`` is the happy terminal; ``rejected`` and any ``*_failed`` mean
# the server gave up on the capture and the title won't change further.
def _is_terminal(status: str) -> bool:
    return status == "analyzed" or status == "rejected" or status.endswith("_failed")


class CapturePoller:
    """Polls the server for late-arriving title/summary and caches them.

    Wired from ``Agent`` and called by ``UploadRetryManager`` on
    ``UploadOutcome.SUCCESS``. The auth client may be ``None`` (e.g.
    ``NoopUploadClient`` path); in that case ``poll`` is a no-op.
    """

    def __init__(
        self,
        auth_client,
        captures_dir: Path,
        executor: ThreadPoolExecutor,
        clock: Callable[[], datetime] | None = None,
        schedule: tuple[float, ...] = DEFAULT_POLL_SCHEDULE,
    ) -> None:
        self._auth_client = auth_client
        self._captures_dir = captures_dir
        self._executor = executor
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._schedule = schedule

    async def poll(self, rec_dir: Path, server_capture_id: str) -> None:
        """Run the sparse poll schedule for one capture.

        Stops on the first cached title (and any terminal status thereafter),
        or after the last scheduled tick. Silent on errors — polling failures
        leave the local placeholder in place; the webapp shows live state if
        the user clicks through.
        """
        if self._auth_client is None:
            return

        cached = False
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

            # Stop once we've cached or the server reports terminal —
            # title/summary won't change further after either point, so
            # any remaining ticks are wasted GETs.
            if cached or _is_terminal(status):
                log.debug(
                    "[poller] stopping id=%s status=%s cached=%s",
                    server_capture_id, status, cached,
                )
                return

        if not cached:
            log.debug(
                "[poller] schedule exhausted id=%s without progressed status",
                server_capture_id,
            )

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
