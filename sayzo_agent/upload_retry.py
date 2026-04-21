"""Orchestrates upload retries, global pauses, and notification throttling.

Both the live pipeline and the periodic sweep go through `try_upload`. This
module owns:
 - Per-capture `metadata.upload` state in record.json
 - Global `.upload_state.json` sidecar tracking credit / auth pauses
 - One-toast-per-incident notification throttling
 - Directory scanning + backoff scheduling for the retry sweep

Imports from `retry` for pure classification logic; nothing imported from here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import UploadConfig
from .models import ConversationRecord
from .notify import Notifier
from .retry import (
    STATUS_IN_FLIGHT,
    UploadOutcome,
    classify_exception,
    is_due,
    is_terminal,
    reconcile_in_flight,
    record_attempt_result,
    record_attempt_start,
)
from .sink import deserialize_record, serialize_record
from .upload import UploadClient

log = logging.getLogger(__name__)


PAUSE_STATE_SCHEMA_VERSION = 1


@dataclass
class PauseState:
    """Global pause state persisted at {captures_dir}/.upload_state.json.

    Notification throttling is implicit in the state machine: a transition
    from "not blocked" to "blocked" fires the toast; calls while already
    blocked are silent.
    """

    credit_blocked_until: Optional[datetime] = None
    auth_blocked: bool = False

    @classmethod
    def from_json(cls, data: dict) -> "PauseState":
        val = data.get("credit_blocked_until")
        until: Optional[datetime] = None
        if val:
            try:
                until = datetime.fromisoformat(val)
            except Exception:
                until = None
        return cls(
            credit_blocked_until=until,
            auth_blocked=bool(data.get("auth_blocked", False)),
        )

    def to_json(self) -> dict:
        return {
            "credit_blocked_until": (
                self.credit_blocked_until.isoformat() if self.credit_blocked_until else None
            ),
            "auth_blocked": self.auth_blocked,
            "schema_version": PAUSE_STATE_SCHEMA_VERSION,
        }


def read_record_from_dir(rec_dir: Path) -> ConversationRecord:
    """Read record.json from a capture directory into a ConversationRecord."""
    with (rec_dir / "record.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return deserialize_record(data)


def write_record_atomic(rec_dir: Path, record: ConversationRecord) -> None:
    """Write record.json via temp-file + os.replace (atomic on Windows + POSIX)."""
    target = rec_dir / "record.json"
    tmp = rec_dir / f"record.json.tmp-{os.getpid()}-{time.monotonic_ns()}"
    payload = json.dumps(serialize_record(record), indent=2, ensure_ascii=False)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


class UploadRetryManager:
    def __init__(
        self,
        captures_dir: Path,
        upload_client: UploadClient,
        notifier: Notifier,
        executor: ThreadPoolExecutor,
        config: UploadConfig,
        auth_client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._captures_dir = captures_dir
        self._upload_client = upload_client
        self._notifier = notifier
        self._executor = executor
        self._cfg = config
        self._auth_client = auth_client
        self._now = clock or (lambda: datetime.now(timezone.utc))

        self._pause_state = PauseState()
        self._pause_lock = asyncio.Lock()
        self._inflight_rec_ids: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        # Serialize actual HTTP uploads (not metadata updates) so we never
        # saturate the uplink with parallel opus POSTs. Config knob allows
        # >1 if the user ever wants concurrency.
        self._upload_sem = asyncio.Semaphore(max(1, int(self._cfg.max_concurrent_uploads)))

        self._pause_state_path = self._captures_dir / self._cfg.pause_state_filename
        self._pause_loaded = False

    # ------------------------------------------------------------------
    # Pause state persistence
    # ------------------------------------------------------------------

    async def _ensure_pause_state_loaded(self) -> None:
        if self._pause_loaded:
            return
        async with self._pause_lock:
            if self._pause_loaded:
                return
            self._pause_state = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._read_pause_state_from_disk
            )
            self._pause_loaded = True

    def _read_pause_state_from_disk(self) -> PauseState:
        if not self._pause_state_path.exists():
            return PauseState()
        try:
            data = json.loads(self._pause_state_path.read_text(encoding="utf-8"))
            return PauseState.from_json(data)
        except Exception:
            log.warning(
                "[upload] %s is corrupt; starting with empty pause state",
                self._pause_state_path,
            )
            return PauseState()

    async def _persist_pause_state(self) -> None:
        snapshot = self._pause_state.to_json()
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self._write_pause_state_to_disk, snapshot
        )

    def _write_pause_state_to_disk(self, snapshot: dict) -> None:
        self._pause_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._pause_state_path.with_suffix(
            self._pause_state_path.suffix + f".tmp-{os.getpid()}-{time.monotonic_ns()}"
        )
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        os.replace(tmp, self._pause_state_path)

    # ------------------------------------------------------------------
    # Public: global-pause decision
    # ------------------------------------------------------------------

    async def should_attempt(self) -> bool:
        """Return True iff no global pause is active. Auto-clears credit pause
        if its expiry has passed (the toast stays silent on resume)."""
        await self._ensure_pause_state_loaded()
        async with self._pause_lock:
            now = self._now()
            if self._pause_state.credit_blocked_until:
                if now >= self._pause_state.credit_blocked_until:
                    # Auto-resume: clear the credit lockout. No toast on resume.
                    self._pause_state.credit_blocked_until = None
                    await self._persist_pause_state()
                else:
                    return False
            if self._pause_state.auth_blocked:
                return False
            return True

    # ------------------------------------------------------------------
    # Public: try_upload (used by live path + sweep)
    # ------------------------------------------------------------------

    async def try_upload(self, record: ConversationRecord, rec_dir: Path) -> UploadOutcome | None:
        """Run one upload attempt for this record.

        Returns the outcome, or ``None`` if the attempt was skipped because
        another task is already uploading this record. Global pauses produce
        a real ``CREDIT_LIMIT`` / ``AUTH_REQUIRED`` outcome (the record is
        marked blocked on disk).
        """
        rec_id = record.id
        async with self._inflight_lock:
            if rec_id in self._inflight_rec_ids:
                log.debug("[upload] skip id=%s — already in flight", rec_id)
                return None
            self._inflight_rec_ids.add(rec_id)
        try:
            return await self._run_attempt(record, rec_dir)
        finally:
            async with self._inflight_lock:
                self._inflight_rec_ids.discard(rec_id)

    async def _run_attempt(self, record: ConversationRecord, rec_dir: Path) -> UploadOutcome:
        # Global pause gate. If blocked, write the block status to record.json
        # so the user can see why this particular record is waiting.
        if not await self.should_attempt():
            # Determine WHICH pause is active so we can label the record correctly.
            async with self._pause_lock:
                if self._pause_state.credit_blocked_until:
                    blocked_outcome = UploadOutcome.CREDIT_LIMIT
                    msg = "Credit limit reached"
                else:
                    blocked_outcome = UploadOutcome.AUTH_REQUIRED
                    msg = "Authentication required"
            await self._update_record_state(
                rec_dir,
                lambda s: record_attempt_result(
                    s, blocked_outcome, msg, None, self._now(),
                    backoff_secs=self._cfg.transient_backoff_secs,
                    max_permanent_other_attempts=self._cfg.max_permanent_other_attempts,
                ),
            )
            return blocked_outcome

        # Mark in_flight + bump attempts on disk.
        await self._update_record_state(
            rec_dir, lambda s: record_attempt_start(s, self._now())
        )

        # Perform the upload (serialized across the manager via the semaphore).
        outcome: UploadOutcome
        message: str | None = None
        server_capture_id: str | None = None
        async with self._upload_sem:
            try:
                server_capture_id = await self._upload_client.upload(record)
                outcome = UploadOutcome.SUCCESS
            except Exception as exc:
                outcome, message = classify_exception(exc)
                log.warning("[upload] failed id=%s outcome=%s — %s", record.id, outcome.value, message)

        # Persist the outcome on the record.
        await self._update_record_state(
            rec_dir,
            lambda s: record_attempt_result(
                s, outcome, message, server_capture_id, self._now(),
                backoff_secs=self._cfg.transient_backoff_secs,
                max_permanent_other_attempts=self._cfg.max_permanent_other_attempts,
            ),
        )

        # Global-pause + notification side effects.
        if outcome == UploadOutcome.CREDIT_LIMIT:
            await self._handle_credit_limit(message)
        elif outcome == UploadOutcome.AUTH_REQUIRED:
            await self._handle_auth_required(message)

        if outcome == UploadOutcome.SUCCESS:
            log.info("[upload] success id=%s server_id=%s", record.id, server_capture_id or "?")

        return outcome

    # ------------------------------------------------------------------
    # Notification-throttled pause handlers
    # ------------------------------------------------------------------

    async def _handle_credit_limit(self, server_message: str | None) -> None:
        fire_toast = False
        async with self._pause_lock:
            now = self._now()
            current = self._pause_state.credit_blocked_until
            if current is None or current <= now:
                # Transition off → on (either fresh lockout or expired window
                # just got renewed by a fresh 402 — either way notify).
                self._pause_state.credit_blocked_until = now + timedelta(
                    seconds=self._cfg.credit_lockout_secs
                )
                await self._persist_pause_state()
                fire_toast = True
        if fire_toast:
            body = server_message or "You've used all your free Sayzo actions."
            await self._fire_notification("Sayzo: upload paused", body)

    async def _handle_auth_required(self, _server_message: str | None) -> None:
        fire_toast = False
        async with self._pause_lock:
            if not self._pause_state.auth_blocked:
                self._pause_state.auth_blocked = True
                await self._persist_pause_state()
                fire_toast = True
        if fire_toast:
            await self._fire_notification(
                "Sayzo: sign-in required",
                "Please run `sayzo-agent login` to resume uploads.",
            )

    async def _check_auth_recovery(self) -> None:
        """If we're auth-blocked, probe the auth client to see if tokens are
        back. Invalidates the TokenStore cache first so a concurrent
        ``sayzo-agent login`` subprocess is picked up. No-op if no auth client
        was wired (e.g. NoopUploadClient). Silent on resume."""
        if self._auth_client is None:
            return
        async with self._pause_lock:
            if not self._pause_state.auth_blocked:
                return
        store = getattr(self._auth_client, "_store", None)
        if store is not None and hasattr(store, "invalidate_cache"):
            try:
                store.invalidate_cache()
            except Exception:
                log.debug("[upload] token cache invalidation failed", exc_info=True)
        try:
            await self._auth_client.get_token()
        except Exception:
            return  # Still blocked.
        async with self._pause_lock:
            if self._pause_state.auth_blocked:
                self._pause_state.auth_blocked = False
                await self._persist_pause_state()
                log.info("[upload] auth recovered — resuming uploads")

    async def _fire_notification(self, title: str, body: str) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._notifier.notify, title, body
            )
        except Exception:
            log.warning("[upload] notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # Per-record state update helper (routed through the executor)
    # ------------------------------------------------------------------

    async def _update_record_state(
        self,
        rec_dir: Path,
        mutator: Callable[[dict | None], dict],
    ) -> dict:
        """Re-read record.json, mutate metadata.upload, write atomically.

        Dispatched to the single-worker executor so concurrent calls for the
        same record serialize at the thread-pool level. Combined with the
        in-flight set preventing parallel try_upload for one rec_id, this is
        sufficient for torn-write-free metadata mutation.
        """
        loop = asyncio.get_running_loop()

        def _do() -> dict:
            record = read_record_from_dir(rec_dir)
            old_upload = record.metadata.get("upload")
            new_upload = mutator(old_upload)
            record.metadata["upload"] = new_upload
            write_record_atomic(rec_dir, record)
            return new_upload

        return await loop.run_in_executor(self._executor, _do)

    # ------------------------------------------------------------------
    # Sweep (startup + periodic) — stubs; filled in next task.
    # ------------------------------------------------------------------

    async def startup_sweep(self) -> None:
        """Called once at agent start: reconcile stuck in_flight records, then
        drain due records up to `max_uploads_per_sweep`. Runs as a background
        task so it never blocks capture startup."""
        await self._ensure_pause_state_loaded()
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._reconcile_stuck_records
            )
        except Exception:
            log.warning("[upload] startup reconciliation failed", exc_info=True)
        try:
            await self.sweep_once()
        except Exception:
            log.warning("[upload] startup sweep failed", exc_info=True)

    async def sweep_once(self) -> None:
        """One pass over captures_dir, uploading up to max_uploads_per_sweep
        records whose next_attempt_at has passed. Bails early if a 402 during
        this sweep sets the global pause."""
        await self._ensure_pause_state_loaded()
        # If we're auth-blocked, poke the auth client to see if a concurrent
        # `sayzo-agent login` restored tokens. This is the ONLY way we detect
        # recovery since the TokenStore cache would otherwise stay stale.
        await self._check_auth_recovery()
        if not await self.should_attempt():
            log.debug("[upload] sweep skipped — global pause active")
            return

        cap = self._cfg.max_uploads_per_sweep
        budget = cap if cap and cap > 0 else None

        loop = asyncio.get_running_loop()
        due: list[tuple[Path, ConversationRecord]] = await loop.run_in_executor(
            self._executor, self._collect_due_records
        )
        if not due:
            return
        log.info("[upload] sweep: %d due record(s)", len(due))
        uploaded = 0
        for rec_dir, record in due:
            if budget is not None and uploaded >= budget:
                break
            if not await self.should_attempt():
                log.info("[upload] sweep: global pause engaged mid-sweep, bailing")
                break
            outcome = await self.try_upload(record, rec_dir)
            if outcome is None:
                # Skipped because the record is already in flight elsewhere
                # (live path racing the sweep). Don't count toward the budget.
                continue
            uploaded += 1
            # If this record hit the 402 wall, subsequent records in this
            # batch will also be blocked — loop exits on the next
            # should_attempt() check.
            if outcome in (UploadOutcome.CREDIT_LIMIT, UploadOutcome.AUTH_REQUIRED):
                break

    def _reconcile_stuck_records(self) -> None:
        """Synchronous: walk captures_dir, flip any in_flight records to
        failed_transient so the sweep picks them up. Also logs a one-time
        count of legacy records without metadata.upload."""
        if not self._captures_dir.exists():
            return
        now = self._now()
        reconciled = 0
        legacy = 0
        for rec_dir in self._iter_capture_dirs():
            record_path = rec_dir / "record.json"
            if not record_path.exists():
                continue
            try:
                record = read_record_from_dir(rec_dir)
            except Exception:
                log.warning("[upload] skipping corrupt record.json at %s", rec_dir)
                continue
            upload_state = record.metadata.get("upload")
            if upload_state is None:
                legacy += 1
                continue
            if upload_state.get("status") == STATUS_IN_FLIGHT:
                record.metadata["upload"] = reconcile_in_flight(upload_state, now)
                try:
                    write_record_atomic(rec_dir, record)
                    reconciled += 1
                except Exception:
                    log.warning("[upload] failed to reconcile %s", rec_dir, exc_info=True)
        if reconciled:
            log.info("[upload] reconciled %d in_flight record(s) from a crashed run", reconciled)
        if legacy:
            log.info(
                "[upload] found %d legacy capture(s) without upload state — will retry", legacy
            )

    def _collect_due_records(self) -> list[tuple[Path, ConversationRecord]]:
        """Synchronous: scan captures_dir, collect records whose state says
        they're due, skip terminal/corrupt, sort oldest-first by started_at."""
        if not self._captures_dir.exists():
            return []
        now = self._now()
        out: list[tuple[Path, ConversationRecord]] = []
        for rec_dir in self._iter_capture_dirs():
            record_path = rec_dir / "record.json"
            if not record_path.exists():
                continue
            try:
                record = read_record_from_dir(rec_dir)
            except Exception:
                log.warning("[upload] skipping corrupt record.json at %s", rec_dir)
                continue
            upload_state = record.metadata.get("upload")
            if is_terminal(upload_state):
                continue
            if not is_due(upload_state, now):
                continue
            out.append((rec_dir, record))
        out.sort(key=lambda pair: pair[1].started_at)
        return out

    def _iter_capture_dirs(self):
        """Yield per-capture directories under captures_dir, skipping hidden/
        non-directory entries (like .upload_state.json)."""
        try:
            with os.scandir(self._captures_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith("."):
                        continue
                    yield Path(entry.path)
        except FileNotFoundError:
            return
