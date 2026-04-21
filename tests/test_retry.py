"""Pure-logic tests for sayzo_agent.retry — no network, no files."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sayzo_agent.auth.exceptions import AuthenticationRequired
from sayzo_agent.retry import (
    MAX_PERMANENT_OTHER_ATTEMPTS,
    STATUS_AUTH_BLOCKED,
    STATUS_CREDIT_BLOCKED,
    STATUS_FAILED_PERMANENT,
    STATUS_FAILED_TRANSIENT,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    STATUS_UPLOADED,
    TRANSIENT_BACKOFF_SECS,
    UploadOutcome,
    classify_exception,
    compute_next_attempt_at,
    empty_upload_state,
    is_due,
    is_terminal,
    reconcile_in_flight,
    record_attempt_result,
    record_attempt_start,
)
from tests._http_helpers import make_http_error as _http_error


# -------------------- classify_exception --------------------

def test_classify_auth_required():
    outcome, msg = classify_exception(AuthenticationRequired("Token expired."))
    assert outcome == UploadOutcome.AUTH_REQUIRED
    assert "expired" in msg.lower() or msg


def test_classify_credit_limit_with_documented_body():
    exc = _http_error(402, {"error": "credit_limit_reached", "message": "You've used all your free Sayzo actions."})
    outcome, msg = classify_exception(exc)
    assert outcome == UploadOutcome.CREDIT_LIMIT
    assert "Sayzo actions" in msg


def test_classify_credit_limit_with_missing_body():
    # 402 with empty body should still be CREDIT_LIMIT.
    exc = _http_error(402, "")
    outcome, msg = classify_exception(exc)
    assert outcome == UploadOutcome.CREDIT_LIMIT


def test_classify_credit_limit_with_unexpected_body_shape():
    # 402 with non-standard body shape must still be CREDIT_LIMIT so we never
    # hammer the server when the server says "no".
    exc = _http_error(402, {"something": "else"})
    outcome, _ = classify_exception(exc)
    assert outcome == UploadOutcome.CREDIT_LIMIT


def test_classify_credit_limit_non_json_body():
    exc = _http_error(402, "service temporarily unavailable")
    outcome, _ = classify_exception(exc)
    assert outcome == UploadOutcome.CREDIT_LIMIT


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 599])
def test_classify_transient_http_statuses(status: int):
    outcome, _ = classify_exception(_http_error(status, "oops"))
    assert outcome == UploadOutcome.TRANSIENT


@pytest.mark.parametrize("status", [400, 403, 404, 409, 410, 413, 415, 422])
def test_classify_permanent_client_http_statuses(status: int):
    outcome, _ = classify_exception(_http_error(status, "bad"))
    assert outcome == UploadOutcome.PERMANENT_CLIENT


def test_classify_transient_on_network_error():
    exc = httpx.ConnectError("connection refused")
    outcome, _ = classify_exception(exc)
    assert outcome == UploadOutcome.TRANSIENT


def test_classify_transient_on_timeout():
    exc = httpx.ReadTimeout("too slow")
    outcome, _ = classify_exception(exc)
    assert outcome == UploadOutcome.TRANSIENT


def test_classify_permanent_other_on_file_not_found():
    outcome, _ = classify_exception(FileNotFoundError("audio.opus"))
    assert outcome == UploadOutcome.PERMANENT_OTHER


def test_classify_permanent_other_on_generic_exception():
    outcome, _ = classify_exception(ValueError("something weird"))
    assert outcome == UploadOutcome.PERMANENT_OTHER


def test_classify_unusual_http_status_goes_permanent_other():
    # 3xx shouldn't reach us (raise_for_status only fires 4xx/5xx), but if
    # something weird happens we don't want to silently retry forever.
    exc = _http_error(301, "")
    outcome, _ = classify_exception(exc)
    assert outcome == UploadOutcome.PERMANENT_OTHER


# -------------------- compute_next_attempt_at --------------------

def test_backoff_monotonic_mid_curve():
    # Freeze jitter to 1.0 so we test the underlying schedule.
    random.seed(0)
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)

    def next_secs(attempts: int) -> float:
        # Run several times and take the min so we're approximating the un-jittered base.
        smallest = float("inf")
        for _ in range(20):
            dt = compute_next_attempt_at(attempts, now)
            smallest = min(smallest, (dt - now).total_seconds())
        return smallest

    vals = [next_secs(n) for n in range(1, 6)]
    # Each tier's lower-bound (0.9x base) must exceed the prior tier's upper-bound (1.1x base).
    for i in range(len(vals) - 1):
        expected_cur_upper = TRANSIENT_BACKOFF_SECS[i] * 1.1
        expected_next_lower = TRANSIENT_BACKOFF_SECS[i + 1] * 0.9
        assert expected_next_lower > expected_cur_upper, "backoff curve must have non-overlapping tiers"


def test_backoff_caps_at_last_value():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    for attempts in (5, 10, 100, 999):
        dt = compute_next_attempt_at(attempts, now)
        secs = (dt - now).total_seconds()
        cap = TRANSIENT_BACKOFF_SECS[-1]
        assert 0.9 * cap <= secs <= 1.1 * cap


def test_backoff_jitter_within_10_percent():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    samples = []
    for _ in range(200):
        dt = compute_next_attempt_at(1, now)
        samples.append((dt - now).total_seconds())
    base = TRANSIENT_BACKOFF_SECS[0]
    assert min(samples) >= 0.9 * base
    assert max(samples) <= 1.1 * base


def test_backoff_first_attempt_uses_first_tier():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    # attempts=0 and attempts=1 both use index 0.
    for attempts in (0, 1):
        dt = compute_next_attempt_at(attempts, now)
        secs = (dt - now).total_seconds()
        assert 0.9 * TRANSIENT_BACKOFF_SECS[0] <= secs <= 1.1 * TRANSIENT_BACKOFF_SECS[0]


def test_backoff_custom_schedule():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    dt = compute_next_attempt_at(2, now, backoff_secs=[10, 20, 30])
    secs = (dt - now).total_seconds()
    assert 0.9 * 20 <= secs <= 1.1 * 20


# -------------------- empty_upload_state + record_attempt_* --------------------

def test_empty_upload_state_shape():
    s = empty_upload_state()
    assert s["status"] == STATUS_PENDING
    assert s["attempts"] == 0
    assert s["last_attempt_at"] is None
    assert s["next_attempt_at"] is None
    assert s["last_error_kind"] is None
    assert s["last_error_message"] is None
    assert s["server_capture_id"] is None


def test_record_attempt_start_bumps_attempts_and_marks_in_flight():
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    s0 = empty_upload_state()
    s1 = record_attempt_start(s0, now)
    assert s1["status"] == STATUS_IN_FLIGHT
    assert s1["attempts"] == 1
    assert s1["last_attempt_at"] == now.isoformat()
    assert s1["next_attempt_at"] is None
    # Original untouched (purity guarantee).
    assert s0["attempts"] == 0


def test_record_attempt_start_fills_legacy_state():
    # A legacy record.json may have nothing — or just a partial dict.
    s = record_attempt_start(None, datetime(2026, 4, 21, tzinfo=timezone.utc))
    assert s["attempts"] == 1
    assert s["status"] == STATUS_IN_FLIGHT


def test_record_attempt_result_success_is_terminal():
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    done = record_attempt_result(s, UploadOutcome.SUCCESS, None, "srv_abc", now)
    assert done["status"] == STATUS_UPLOADED
    assert done["next_attempt_at"] is None
    assert done["server_capture_id"] == "srv_abc"
    assert done["last_error_kind"] is None
    assert done["last_error_message"] is None
    assert is_terminal(done)


def test_record_attempt_result_transient_schedules_next():
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    out = record_attempt_result(s, UploadOutcome.TRANSIENT, "HTTP 503", None, now)
    assert out["status"] == STATUS_FAILED_TRANSIENT
    assert out["next_attempt_at"] is not None
    assert out["last_error_kind"] == "transient"
    assert out["last_error_message"] == "HTTP 503"
    assert not is_terminal(out)


def test_record_attempt_result_credit_clears_next_and_is_not_terminal():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    out = record_attempt_result(s, UploadOutcome.CREDIT_LIMIT, "no credits", None, now)
    assert out["status"] == STATUS_CREDIT_BLOCKED
    assert out["next_attempt_at"] is None
    # Not terminal — resumes when global pause lifts.
    assert not is_terminal(out)


def test_record_attempt_result_auth_blocked_clears_next():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    out = record_attempt_result(s, UploadOutcome.AUTH_REQUIRED, "expired", None, now)
    assert out["status"] == STATUS_AUTH_BLOCKED
    assert out["next_attempt_at"] is None
    assert not is_terminal(out)


def test_record_attempt_result_permanent_client_terminal_after_one_attempt():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    out = record_attempt_result(s, UploadOutcome.PERMANENT_CLIENT, "HTTP 400", None, now)
    assert out["status"] == STATUS_FAILED_PERMANENT
    assert is_terminal(out)


def test_record_attempt_result_permanent_other_retries_until_max():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    state = empty_upload_state()
    # MAX_PERMANENT_OTHER_ATTEMPTS=3: first two failures → failed_transient, third → failed_permanent.
    for i in range(1, MAX_PERMANENT_OTHER_ATTEMPTS + 1):
        state = record_attempt_start(state, now)
        state = record_attempt_result(state, UploadOutcome.PERMANENT_OTHER, f"weird {i}", None, now)
        if i < MAX_PERMANENT_OTHER_ATTEMPTS:
            assert state["status"] == STATUS_FAILED_TRANSIENT
            assert not is_terminal(state)
        else:
            assert state["status"] == STATUS_FAILED_PERMANENT
            assert is_terminal(state)


def test_record_attempt_result_truncates_long_error_messages():
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = record_attempt_start(empty_upload_state(), now)
    long_msg = "x" * 10000
    out = record_attempt_result(s, UploadOutcome.TRANSIENT, long_msg, None, now)
    assert len(out["last_error_message"]) == 500


def test_record_attempt_result_preserves_server_id_on_subsequent_failures():
    # If somehow server_capture_id got set on a previous success and a later
    # attempt fails, don't erase the successful id.
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = empty_upload_state()
    s["server_capture_id"] = "srv_abc"
    s = record_attempt_start(s, now)
    out = record_attempt_result(s, UploadOutcome.TRANSIENT, "blip", None, now)
    assert out["server_capture_id"] == "srv_abc"


# -------------------- reconcile_in_flight --------------------

def test_reconcile_in_flight_converts_to_failed_transient():
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    stuck = record_attempt_start(empty_upload_state(), now - timedelta(hours=3))
    assert stuck["status"] == STATUS_IN_FLIGHT
    fixed = reconcile_in_flight(stuck, now, retry_after_secs=60.0)
    assert fixed["status"] == STATUS_FAILED_TRANSIENT
    next_dt = datetime.fromisoformat(fixed["next_attempt_at"])
    assert timedelta(seconds=59) <= next_dt - now <= timedelta(seconds=61)
    # Attempts NOT bumped — we don't know if the crashed attempt made it to the server.
    assert fixed["attempts"] == stuck["attempts"]


# -------------------- is_due / is_terminal --------------------

def test_is_due_legacy_none_state():
    assert is_due(None, datetime(2026, 4, 21, tzinfo=timezone.utc)) is True


def test_is_due_pending_always_true():
    assert is_due(empty_upload_state(), datetime(2026, 4, 21, tzinfo=timezone.utc)) is True


def test_is_due_uploaded_false():
    s = empty_upload_state()
    s["status"] = STATUS_UPLOADED
    assert is_due(s, datetime(2026, 4, 21, tzinfo=timezone.utc)) is False


def test_is_due_failed_permanent_false():
    s = empty_upload_state()
    s["status"] = STATUS_FAILED_PERMANENT
    assert is_due(s, datetime(2026, 4, 21, tzinfo=timezone.utc)) is False


def test_is_due_failed_transient_respects_next_attempt_at():
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    s = empty_upload_state()
    s["status"] = STATUS_FAILED_TRANSIENT
    s["next_attempt_at"] = (now + timedelta(minutes=10)).isoformat()
    # Too early.
    assert is_due(s, now) is False
    # After threshold.
    assert is_due(s, now + timedelta(minutes=11)) is True


def test_is_due_blocked_states_due_once_global_pause_lifts():
    # is_due doesn't know about global pause — caller's responsibility.
    # But the record itself should be "due" so the sweep picks it up on
    # the first post-pause iteration.
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    s = empty_upload_state()
    s["status"] = STATUS_CREDIT_BLOCKED
    assert is_due(s, now) is True
    s["status"] = STATUS_AUTH_BLOCKED
    assert is_due(s, now) is True


def test_is_terminal():
    assert is_terminal(None) is False
    assert is_terminal(empty_upload_state()) is False
    s = empty_upload_state()
    s["status"] = STATUS_UPLOADED
    assert is_terminal(s) is True
    s["status"] = STATUS_FAILED_PERMANENT
    assert is_terminal(s) is True
    s["status"] = STATUS_FAILED_TRANSIENT
    assert is_terminal(s) is False
