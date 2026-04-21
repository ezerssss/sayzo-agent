"""Pure classification + scheduling logic for upload retries.

No I/O, no network — safe to unit-test without fixtures. The UploadRetryManager
in upload_retry.py calls into this module to decide whether a failed upload
should be retried, how long to wait, and what status to persist on the record.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from enum import Enum

import httpx

from .auth.exceptions import AuthenticationRequired


class UploadOutcome(str, Enum):
    SUCCESS = "success"
    CREDIT_LIMIT = "credit_limit"
    AUTH_REQUIRED = "auth_required"
    TRANSIENT = "transient"
    PERMANENT_CLIENT = "permanent_client"
    PERMANENT_OTHER = "permanent_other"


# Status values written to record.json's metadata.upload.status field.
STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED_TRANSIENT = "failed_transient"
STATUS_FAILED_PERMANENT = "failed_permanent"
STATUS_CREDIT_BLOCKED = "credit_blocked"
STATUS_AUTH_BLOCKED = "auth_blocked"

TERMINAL_STATUSES = frozenset({STATUS_UPLOADED, STATUS_FAILED_PERMANENT})

# Exponential-ish backoff curve for transient failures. attempt N (1-indexed)
# uses index min(N-1, len-1). Plenty for a laptop that came back from a month
# offline — at the 6h cap a single record retries ~4x/day.
TRANSIENT_BACKOFF_SECS: list[int] = [300, 900, 3600, 10800, 21600]  # 5m, 15m, 1h, 3h, 6h

# After this many PERMANENT_OTHER attempts, give up on the record.
# PERMANENT_CLIENT is terminal after 1 attempt (the server already said no).
MAX_PERMANENT_OTHER_ATTEMPTS = 3

# Retry-after window when un-sticking a crashed in_flight record at startup.
RECONCILE_RETRY_SECS = 60.0


def classify_exception(exc: BaseException) -> tuple[UploadOutcome, str]:
    """Map a raised exception from AuthenticatedUploadClient.upload() to a
    canonical outcome + human-readable message.

    HTTP 402 always maps to CREDIT_LIMIT regardless of body shape — the server
    can't drift the semantics of 402 without breaking billing. Body sniff is
    best-effort for the error message.
    """
    if isinstance(exc, AuthenticationRequired):
        return UploadOutcome.AUTH_REQUIRED, str(exc) or "Authentication required"

    if isinstance(exc, FileNotFoundError):
        return UploadOutcome.PERMANENT_OTHER, f"File not found: {exc}"

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 402:
            try:
                body = exc.response.json()
                if isinstance(body, dict) and body.get("error") == "credit_limit_reached":
                    msg = body.get("message") or "Credit limit reached"
                    return UploadOutcome.CREDIT_LIMIT, msg
            except Exception:
                pass
            return UploadOutcome.CREDIT_LIMIT, f"HTTP 402: {_safe_text(exc.response)[:200]}"
        if status in (408, 429) or 500 <= status < 600:
            return UploadOutcome.TRANSIENT, f"HTTP {status}: {_safe_text(exc.response)[:200]}"
        if 400 <= status < 500:
            return UploadOutcome.PERMANENT_CLIENT, f"HTTP {status}: {_safe_text(exc.response)[:200]}"
        return UploadOutcome.PERMANENT_OTHER, f"HTTP {status}: {_safe_text(exc.response)[:200]}"

    if isinstance(exc, httpx.RequestError):
        return UploadOutcome.TRANSIENT, f"{type(exc).__name__}: {exc}"

    return UploadOutcome.PERMANENT_OTHER, f"{type(exc).__name__}: {exc}"


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:
        return ""


def compute_next_attempt_at(
    attempts: int,
    now: datetime,
    backoff_secs: list[int] | None = None,
) -> datetime:
    """Given the total attempt count including the one that just failed,
    return the UTC datetime at which the next retry is allowed.

    Applies ±10% jitter so a backlog of records that all failed at the same
    instant don't stampede on reconnect.
    """
    schedule = backoff_secs if backoff_secs else TRANSIENT_BACKOFF_SECS
    if not schedule:
        return now + timedelta(seconds=300)
    idx = min(max(attempts - 1, 0), len(schedule) - 1)
    base = schedule[idx]
    jitter = random.uniform(0.9, 1.1)
    return now + timedelta(seconds=base * jitter)


def empty_upload_state() -> dict:
    """Default state for a freshly-written record that has never been attempted."""
    return {
        "status": STATUS_PENDING,
        "attempts": 0,
        "last_attempt_at": None,
        "next_attempt_at": None,
        "last_error_kind": None,
        "last_error_message": None,
        "server_capture_id": None,
    }


def record_attempt_start(state: dict | None, now: datetime) -> dict:
    """Mutate (return new) state to mark an upload attempt is beginning.
    Bumps attempts, sets status=in_flight, clears next_attempt_at."""
    new = dict(state) if state else empty_upload_state()
    # Fill in any missing keys from legacy records.
    for k, v in empty_upload_state().items():
        new.setdefault(k, v)
    new["status"] = STATUS_IN_FLIGHT
    new["attempts"] = int(new.get("attempts") or 0) + 1
    new["last_attempt_at"] = now.isoformat()
    new["next_attempt_at"] = None
    return new


def record_attempt_result(
    state: dict | None,
    outcome: UploadOutcome,
    message: str | None,
    server_capture_id: str | None,
    now: datetime,
    backoff_secs: list[int] | None = None,
    max_permanent_other_attempts: int = MAX_PERMANENT_OTHER_ATTEMPTS,
) -> dict:
    """Apply the outcome of one upload attempt to the state dict and return
    the updated version. Does not mutate the input.

    CREDIT_LIMIT and AUTH_REQUIRED don't schedule next_attempt_at on the
    record — the global pause controls when uploads resume, at which point
    these records become due immediately.
    """
    new = dict(state) if state else empty_upload_state()
    for k, v in empty_upload_state().items():
        new.setdefault(k, v)
    attempts = int(new.get("attempts") or 0)

    if outcome == UploadOutcome.SUCCESS:
        new["status"] = STATUS_UPLOADED
        new["next_attempt_at"] = None
        new["last_error_kind"] = None
        new["last_error_message"] = None
        if server_capture_id:
            new["server_capture_id"] = server_capture_id
        return new

    # Failure path: always record the kind + message (truncated).
    new["last_error_kind"] = outcome.value
    new["last_error_message"] = (message or "")[:500] or None

    if outcome == UploadOutcome.CREDIT_LIMIT:
        new["status"] = STATUS_CREDIT_BLOCKED
        new["next_attempt_at"] = None
    elif outcome == UploadOutcome.AUTH_REQUIRED:
        new["status"] = STATUS_AUTH_BLOCKED
        new["next_attempt_at"] = None
    elif outcome == UploadOutcome.TRANSIENT:
        new["status"] = STATUS_FAILED_TRANSIENT
        new["next_attempt_at"] = compute_next_attempt_at(attempts, now, backoff_secs).isoformat()
    elif outcome == UploadOutcome.PERMANENT_CLIENT:
        new["status"] = STATUS_FAILED_PERMANENT
        new["next_attempt_at"] = None
    elif outcome == UploadOutcome.PERMANENT_OTHER:
        if attempts >= max_permanent_other_attempts:
            new["status"] = STATUS_FAILED_PERMANENT
            new["next_attempt_at"] = None
        else:
            new["status"] = STATUS_FAILED_TRANSIENT
            new["next_attempt_at"] = compute_next_attempt_at(attempts, now, backoff_secs).isoformat()

    return new


def reconcile_in_flight(state: dict, now: datetime, retry_after_secs: float = RECONCILE_RETRY_SECS) -> dict:
    """Used at agent startup: any record still at status=in_flight is
    assumed to be from a crashed process. Flip to failed_transient with a
    short retry window. Don't bump attempts — we can't tell if the prior
    attempt counted on the server or not."""
    new = dict(state)
    for k, v in empty_upload_state().items():
        new.setdefault(k, v)
    new["status"] = STATUS_FAILED_TRANSIENT
    new["next_attempt_at"] = (now + timedelta(seconds=retry_after_secs)).isoformat()
    return new


def is_due(state: dict | None, now: datetime) -> bool:
    """Whether a record is eligible for a retry attempt right now. Does NOT
    consult the global pause — that's the caller's job."""
    if state is None:
        return True  # Legacy record without metadata.upload — treat as pending.
    status = state.get("status") or STATUS_PENDING
    if status in TERMINAL_STATUSES:
        return False
    if status in (STATUS_CREDIT_BLOCKED, STATUS_AUTH_BLOCKED):
        # Ready as soon as the global pause lifts; the caller gates on that.
        return True
    if status == STATUS_PENDING or status == STATUS_IN_FLIGHT:
        return True
    next_at = state.get("next_attempt_at")
    if not next_at:
        return True
    try:
        dt = datetime.fromisoformat(next_at)
    except Exception:
        return True
    return now >= dt


def is_terminal(state: dict | None) -> bool:
    if state is None:
        return False
    return (state.get("status") or STATUS_PENDING) in TERMINAL_STATUSES
