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
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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
from .sink import read_record_from_dir, write_record_atomic
from .upload import UploadClient

log = logging.getLogger(__name__)


PAUSE_STATE_SCHEMA_VERSION = 1

# Freshness window (seconds) for the post-capture insight card (v3.14).
# A capture whose immediate LIVE upload failed and got picked up by the
# sweep moments later is still FRESH and deserves the card — the
# sweep-suppression only exists to keep a BACKLOG DRAIN (many hours-old
# captures uploading at once) from spamming cards. So the card fires for a
# live capture OR a capture that ended within this window; older backlog
# captures stay silent as before. Matches the insight feature's 1 h
# staleness horizon (CapturePoller._INSIGHT_DEFER_MAX_SECS). See
# [[project_mac_no_insight_card]].
INSIGHT_FRESHNESS_GATE_SECS = 3600.0


def _per_record_block_body(
    record: "ConversationRecord | None",
    *,
    reason: str,
    fallback: str,
    suffix: str | None = None,
) -> str:
    """Compose a toast body that names the specific capture that got blocked.

    Centralises the "[Title] couldn't upload — [reason]. [fallback / suffix]"
    shape so credit + auth toasts read consistently. ``record`` may be None
    for callers that don't have one in hand; the body falls back to a generic
    line in that case.
    """
    reason_clean = (reason or "").strip().rstrip(".") or "upload paused"
    if record is None:
        return f"{reason_clean}. {fallback}".strip()
    title_hint = ((record.title or "").strip() or "Your latest meeting").rstrip(".")
    tail = suffix if suffix is not None else fallback
    return f"{title_hint} couldn't upload — {reason_clean}. {tail}".strip()


@dataclass
class PauseState:
    """Global pause state persisted at {captures_dir}/.upload_state.json.

    The credit + auth states arm the sweep gate so background retries don't
    hammer a known-blocked endpoint. Live-path uploads bypass the gate and
    re-test the server every time, so a stale local pause never silently
    rejects a fresh capture after the user tops up credits server-side.
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
        webapp_base_url: str | None = None,
        on_upload_success: Callable[[Path, str, bool], Awaitable[None]] | None = None,
        notify_capture_saved: bool = True,
        feedback_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._captures_dir = captures_dir
        self._upload_client = upload_client
        self._notifier = notifier
        self._executor = executor
        self._cfg = config
        self._auth_client = auth_client
        self._now = clock or (lambda: datetime.now(timezone.utc))
        # Base URL for the post-upload "Open in Sayzo" deep-link.
        # ``{webapp_base_url}/app/conversations/{server_capture_id}`` — see
        # reference_deeplink_url memory. ``None`` when there's no auth /
        # NoopUploadClient path; the success toast is skipped cleanly.
        self._webapp_base_url = webapp_base_url
        # Optional hook spawned fire-and-forget after a successful upload —
        # must accept (rec_dir, server_capture_id, owns_toast) and swallow its
        # own errors. See ``CapturePoller.poll``. ``owns_toast`` tells the
        # poller whether IT owns the single per-capture toast (the post-capture
        # insight card) — true only for a live capture with the feedback
        # feature on, so the decision stays complementary to the saved-toast
        # below and we never double-toast.
        self._on_upload_success = on_upload_success
        self._notify_capture_saved = notify_capture_saved
        # Live read of ``Config.notify_capture_feedback``. When it returns
        # True, the post-capture insight card (fired later by the poller)
        # REPLACES the immediate "Conversation saved" toast — see the gate below.
        # None ⇒ feature off (preserves pre-v3.10 behavior + keeps tests simple).
        self._feedback_enabled = feedback_enabled

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
        # Keep strong refs to in-flight on_upload_success tasks. asyncio
        # only weakly references running tasks via the event loop, so a
        # task with no other ref can be GC'd mid-await; stash here and
        # self-clean on completion. See asyncio.create_task docs.
        self._success_tasks: set[asyncio.Task] = set()
        # Optional "Sign in" button handler for the auth-required toast.
        # Wired post-construction by __main__ (it needs the tray) — opens
        # Settings → Account so the user runs the desktop sign-in that
        # actually clears the auth block (a web login wouldn't refresh the
        # agent's OAuth token). None ⇒ fall back to a plain button-less toast.
        self._on_sign_in_requested: Callable[[], None] | None = None

    def set_sign_in_callback(self, cb: Callable[[], None] | None) -> None:
        """Wire (or clear) the auth-required toast's "Sign in" button handler.

        Set by ``__main__::_build_pipeline_state`` after the tray exists. The
        callback fires from the HUD reader thread, so it must be cheap +
        thread-safe (the wired closure just sets a ``TrayState`` field + event).
        """
        self._on_sign_in_requested = cb

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
                self._pause_state_path, exc_info=True,
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

    async def clear_credit_pause(self) -> bool:
        """Drop any active credit lockout so the next upload attempt actually
        contacts the server. Called when the user clicks Try Again — they've
        topped up server-side and the local 24h timer would otherwise keep
        rejecting attempts. If credits really are still out the next 402 will
        re-arm the pause via ``_handle_credit_limit``. Returns True if a pause
        was actually cleared."""
        await self._ensure_pause_state_loaded()
        async with self._pause_lock:
            if self._pause_state.credit_blocked_until is None:
                return False
            self._pause_state.credit_blocked_until = None
            await self._persist_pause_state()
        log.info("[upload] credit pause cleared by user retry request")
        return True

    # ------------------------------------------------------------------
    # Public: try_upload (used by live path + sweep)
    # ------------------------------------------------------------------

    async def try_upload(
        self,
        record: ConversationRecord,
        rec_dir: Path,
        bypass_pause_gate: bool = False,
        live: bool = False,
    ) -> UploadOutcome | None:
        """Run one upload attempt for this record.

        Returns the outcome, or ``None`` if the attempt was skipped because
        another task is already uploading this record. Global pauses produce
        a real ``CREDIT_LIMIT`` / ``AUTH_REQUIRED`` outcome (the record is
        marked blocked on disk).

        ``bypass_pause_gate=True`` is for the live-path call from
        ``app._process_session``: a fresh user-driven upload should always
        contact the server so a stale pause doesn't reject credits the user
        already topped up. The sweep stays gated (default False) to avoid
        hammering a known-blocked endpoint with a backlog of records.

        ``live=True`` marks this call as coming from the live arm/capture
        path (a session that just finished). Currently this only gates the
        "Conversation saved to Sayzo" success toast — sweep-triggered successes
        stay silent so that draining a backlog of "couldn't upload" captures
        (automatic sweep or the Settings → Captures Try Again button) doesn't
        fire a burst of toasts the user has no use for after the fact. The
        visual confirmation for sweep success lives in the Captures pane row
        flipping out of the "Couldn't upload" state.
        """
        rec_id = record.id
        async with self._inflight_lock:
            if rec_id in self._inflight_rec_ids:
                log.debug("[upload] skip id=%s — already in flight", rec_id)
                return None
            self._inflight_rec_ids.add(rec_id)
        try:
            return await self._run_attempt(record, rec_dir, bypass_pause_gate, live)
        finally:
            async with self._inflight_lock:
                self._inflight_rec_ids.discard(rec_id)

    async def _run_attempt(
        self,
        record: ConversationRecord,
        rec_dir: Path,
        bypass_pause_gate: bool = False,
        live: bool = False,
    ) -> UploadOutcome:
        # Global pause gate. Live attempts (``bypass_pause_gate=True``) skip
        # this so a stale local pause never silently rejects a brand-new
        # capture — we always re-test the server, and a fresh 402 re-arms
        # the pause for the sweep. The gate still protects the sweep from
        # hammering a known-blocked endpoint.
        if not bypass_pause_gate and not await self.should_attempt():
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
        server_response: dict | None = None
        async with self._upload_sem:
            try:
                server_response = await self._upload_client.upload(record)
                outcome = UploadOutcome.SUCCESS
            except Exception as exc:
                outcome, message = classify_exception(exc)
                log.warning("[upload] failed id=%s outcome=%s — %s", record.id, outcome.value, message)

        server_capture_id: str | None = None
        if isinstance(server_response, dict):
            cid = server_response.get("capture_id")
            if isinstance(cid, str):
                server_capture_id = cid

        # Persist the outcome on the record.
        await self._update_record_state(
            rec_dir,
            lambda s: record_attempt_result(
                s, outcome, message, server_capture_id, self._now(),
                backoff_secs=self._cfg.transient_backoff_secs,
                max_permanent_other_attempts=self._cfg.max_permanent_other_attempts,
            ),
        )

        # Post-capture feedback decides who owns the single per-capture toast.
        # Read the flag live (a Settings toggle / in-card "Stop showing these"
        # applies to the next capture). ``owns_toast`` → the poller fires the
        # insight card (or its fallback) and the immediate saved toast below is
        # suppressed. The two decisions are complementary so a capture never
        # gets two toasts.
        #
        # v3.14 freshness refinement: owns_toast is True for a live capture OR
        # a still-FRESH one (ended < INSIGHT_FRESHNESS_GATE_SECS ago). This is
        # what makes the card fire on captures whose live upload failed and got
        # swept seconds later — the common macOS case (see
        # [[project_mac_no_insight_card]]) — while a genuine backlog drain of
        # hours-old captures stays silent. Was ``live and feedback_on``.
        feedback_on = bool(self._feedback_enabled and self._feedback_enabled())
        capture_fresh = False
        if record.ended_at is not None:
            try:
                age = (self._now() - record.ended_at).total_seconds()
                capture_fresh = 0 <= age < INSIGHT_FRESHNESS_GATE_SECS
            except Exception:
                # Naive/aware mismatch or a bad clock — fall back to the
                # live-only behavior rather than risk a stale card.
                capture_fresh = False
        owns_toast = bool(feedback_on and (live or capture_fresh))

        # Fire-and-forget post-upload hook (see CapturePoller.poll).
        if (
            outcome == UploadOutcome.SUCCESS
            and server_capture_id
            and self._on_upload_success is not None
        ):
            task = asyncio.create_task(
                self._on_upload_success(rec_dir, server_capture_id, owns_toast)
            )
            self._success_tasks.add(task)
            task.add_done_callback(self._success_tasks.discard)

        # Global-pause + notification side effects.
        if outcome == UploadOutcome.CREDIT_LIMIT:
            await self._handle_credit_limit(message, record)
        elif outcome == UploadOutcome.AUTH_REQUIRED:
            await self._handle_auth_required(message, record)

        if outcome == UploadOutcome.SUCCESS:
            log.info("[upload] success id=%s server_id=%s", record.id, server_capture_id or "?")
            # Live-path only. Sweep successes (auto + user-triggered Try Again)
            # stay silent to avoid a toast burst when a backlog drains; the
            # Captures pane row flipping state is the user-visible signal there.
            # Suppressed when ``feedback_on`` — the poller's post-capture
            # insight card becomes the single per-capture toast (it deep-links
            # too), with a fallback saved toast when no insight is produced.
            if (
                live
                and server_capture_id
                and self._webapp_base_url
                and self._notify_capture_saved
                and not feedback_on
            ):
                url = (
                    self._webapp_base_url.rstrip("/")
                    + f"/app/conversations/{server_capture_id}"
                )
                def _open_in_sayzo(u: str = url) -> None:
                    try:
                        webbrowser.open(u)
                    except Exception:
                        log.debug("[upload] webbrowser.open failed for %r", u, exc_info=True)

                try:
                    self._notifier.notify_actionable(
                        "Conversation saved to Sayzo",
                        "Open it to see your transcript and coaching.",
                        button_label="Open in Sayzo",
                        on_pressed=_open_in_sayzo,
                        expire_after_secs=30.0,
                    )
                except Exception:
                    log.debug("[upload] saved-toast failed", exc_info=True)

        return outcome

    # ------------------------------------------------------------------
    # Notification-throttled pause handlers
    # ------------------------------------------------------------------

    async def _handle_credit_limit(
        self,
        server_message: str | None,
        record: ConversationRecord | None = None,
    ) -> None:
        """Re-arm the credit pause and fire a per-record toast.

        Fires every time a real 402 lands so the user gets feedback on each
        specific upload that's blocked, not only the first one. The pause
        window is (re-)extended each time so the sweep stays gated. The live
        path bypasses the gate, so subsequent new captures still reach the
        server and re-fire this handler on their own 402."""
        async with self._pause_lock:
            now = self._now()
            self._pause_state.credit_blocked_until = now + timedelta(
                seconds=self._cfg.credit_lockout_secs
            )
            await self._persist_pause_state()
        base = (server_message or "You've used all your free Sayzo actions.").strip()
        body = _per_record_block_body(
            record,
            reason=base,
            fallback="Your meeting is saved locally and we'll retry once credits are available.",
        )
        await self._fire_notification("Sayzo: upload paused", body)

    async def _handle_auth_required(
        self,
        _server_message: str | None,
        record: ConversationRecord | None = None,
    ) -> None:
        async with self._pause_lock:
            self._pause_state.auth_blocked = True
            await self._persist_pause_state()
        body = _per_record_block_body(
            record,
            reason="Your Sayzo session expired",
            fallback=(
                "Open Settings → Account to sign in again — captures keep "
                "saving locally until then."
            ),
            suffix="Open Settings → Account to sign in again.",
        )
        cb = self._on_sign_in_requested
        if cb is not None:
            # Actionable toast: the "Sign in" button opens Settings → Account
            # (the desktop sign-in that actually clears this block). Falls back
            # to the plain toast below if the button can't be wired (e.g. the
            # NoopNotifier / ``sayzo-agent run`` path leaves the callback None).
            await self._fire_signin_actionable("Sayzo: sign-in required", body, cb)
        else:
            await self._fire_notification("Sayzo: sign-in required", body)

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

    async def _fire_signin_actionable(
        self, title: str, body: str, on_pressed: Callable[[], None]
    ) -> None:
        """Fire the auth-required toast with a "Sign in" button.

        Routed through the executor like ``_fire_notification`` so the stdin
        write to the HUD subprocess never blocks the loop. ``on_pressed`` is
        invoked later from the HUD reader thread when the button is clicked.
        """
        def _fire() -> None:
            self._notifier.notify_actionable(
                title,
                body,
                button_label="Sign in",
                on_pressed=on_pressed,
                expire_after_secs=60.0,
            )

        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, _fire)
        except Exception:
            log.warning("[upload] sign-in toast failed", exc_info=True)

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
                log.warning(
                    "[upload] skipping corrupt record.json at %s during reconcile",
                    rec_dir, exc_info=True,
                )
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
                log.warning(
                    "[upload] skipping corrupt record.json at %s during sweep",
                    rec_dir, exc_info=True,
                )
                continue
            # Dropped-stub records (cheap-gate fail, non-English, empty
            # transcript) have no audio.opus and were never intended for
            # upload. New stubs land with metadata.upload.status =
            # STATUS_DISCARDED_LOCALLY which is_terminal() catches; this
            # extra guard handles legacy stubs from before that change so
            # we don't re-warn about them on every sweep.
            if record.metadata.get("dropped"):
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
