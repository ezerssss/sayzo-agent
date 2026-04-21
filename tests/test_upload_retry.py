"""Integration tests for UploadRetryManager.

These exercise the full retry-manager pipeline — classification, on-disk state
updates, global pause transitions, notification throttling, sweep behavior —
with a mocked upload client that we control per-test.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest

from sayzo_agent.auth.exceptions import AuthenticationRequired
from sayzo_agent.config import UploadConfig
from sayzo_agent.models import ConversationRecord
from sayzo_agent.retry import (
    STATUS_AUTH_BLOCKED,
    STATUS_CREDIT_BLOCKED,
    STATUS_FAILED_PERMANENT,
    STATUS_FAILED_TRANSIENT,
    STATUS_IN_FLIGHT,
    STATUS_PENDING,
    STATUS_UPLOADED,
    UploadOutcome,
    empty_upload_state,
)
from sayzo_agent.sink import deserialize_record, serialize_record
from sayzo_agent.upload_retry import (
    PauseState,
    UploadRetryManager,
    read_record_from_dir,
)


# ================================================================
# Helpers
# ================================================================


from tests._http_helpers import make_http_error as _http_error


@dataclass
class _QueuedOutcome:
    kind: str
    kwargs: dict = field(default_factory=dict)


class MockUploadClient:
    """An UploadClient whose response each call can be scripted. If the queue
    runs dry the client defaults to success."""

    def __init__(self) -> None:
        self.queue: list[_QueuedOutcome] = []
        self.calls: list[str] = []  # record ids in order

    def enqueue(self, kind: str, **kwargs: Any) -> None:
        self.queue.append(_QueuedOutcome(kind, kwargs))

    async def upload(self, record: ConversationRecord) -> str | None:
        self.calls.append(record.id)
        if not self.queue:
            return f"srv_{record.id}"
        out = self.queue.pop(0)
        if out.kind == "success":
            return out.kwargs.get("capture_id", f"srv_{record.id}")
        if out.kind == "credit_limit":
            raise _http_error(
                402,
                {
                    "error": "credit_limit_reached",
                    "message": out.kwargs.get(
                        "message",
                        "You've used all your free Sayzo actions. Request full access to keep going.",
                    ),
                },
            )
        if out.kind == "auth_required":
            raise AuthenticationRequired(out.kwargs.get("message", "Token expired"))
        if out.kind == "transient_network":
            raise httpx.ConnectError("network unreachable")
        if out.kind == "transient_5xx":
            raise _http_error(out.kwargs.get("status", 503), "oops")
        if out.kind == "permanent_client":
            raise _http_error(out.kwargs.get("status", 400), out.kwargs.get("body", "bad"))
        if out.kind == "file_missing":
            raise FileNotFoundError("audio.opus missing")
        if out.kind == "unexpected":
            raise out.kwargs.get("exc", ValueError("weird"))
        raise ValueError(f"unknown mock outcome {out.kind}")


class MockNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> None:
        self.calls.append((title, body))


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, when: datetime) -> None:
        self._now = when

    def __call__(self) -> datetime:
        return self._now


class FakeAuthClient:
    """Mimics the parts of AuthenticatedClient the retry manager probes:
    `_store.invalidate_cache()` and `await get_token()`. `queue_token_result`
    scripts success/failure for each call."""

    def __init__(self) -> None:
        self._store = SimpleNamespace(invalidate_cache=self._invalidate)
        self._invalidations = 0
        self._token_queue: list[bool] = []  # True = success, False = fail

    def _invalidate(self) -> None:
        self._invalidations += 1

    def queue_token_result(self, ok: bool) -> None:
        self._token_queue.append(ok)

    async def get_token(self) -> str:
        ok = self._token_queue.pop(0) if self._token_queue else True
        if ok:
            return "fake-token"
        raise AuthenticationRequired("still expired")


def _make_record(rec_id: str, started_at: datetime) -> ConversationRecord:
    return ConversationRecord(
        id=rec_id,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=1),
        transcript=[],
        title=f"Session {rec_id}",
        summary="Test",
        audio_path="audio.opus",
        relevant_span=(0.0, 1.0),
        metadata={"close_reason": "joint_silence", "upload": empty_upload_state()},
    )


def _write_capture(
    captures_dir: Path,
    rec_id: str,
    started_at: datetime,
    upload_state: Optional[dict] = None,
    include_audio: bool = True,
) -> tuple[Path, ConversationRecord]:
    rec_dir = captures_dir / rec_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    if include_audio:
        (rec_dir / "audio.opus").write_bytes(b"fake-opus-bytes")
    record = _make_record(rec_id, started_at)
    if upload_state is None:
        # Leave default empty_upload_state from _make_record.
        pass
    elif upload_state == "__legacy__":
        # Simulate a record written before this change: no "upload" key.
        record.metadata.pop("upload", None)
    else:
        record.metadata["upload"] = upload_state
    (rec_dir / "record.json").write_text(
        json.dumps(serialize_record(record), indent=2), encoding="utf-8"
    )
    return rec_dir, record


def _read_upload_state(rec_dir: Path) -> dict:
    return read_record_from_dir(rec_dir).metadata.get("upload") or {}


@pytest.fixture
def env(tmp_path):
    captures_dir = tmp_path / "captures"
    captures_dir.mkdir()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-retry")
    clock = FakeClock(datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc))
    cfg = UploadConfig()
    upload = MockUploadClient()
    notifier = MockNotifier()
    auth_client = FakeAuthClient()
    mgr = UploadRetryManager(
        captures_dir=captures_dir,
        upload_client=upload,
        notifier=notifier,
        executor=executor,
        config=cfg,
        auth_client=auth_client,
        clock=clock,
    )
    try:
        yield SimpleNamespace(
            captures_dir=captures_dir,
            executor=executor,
            clock=clock,
            cfg=cfg,
            upload=upload,
            notifier=notifier,
            auth_client=auth_client,
            mgr=mgr,
            tmp_path=tmp_path,
        )
    finally:
        executor.shutdown(wait=True)


# ================================================================
# Live-path behavior
# ================================================================


async def test_live_upload_success_updates_metadata(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec1", env.clock())
    env.upload.enqueue("success", capture_id="srv_abc")
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.SUCCESS
    state = _read_upload_state(rec_dir)
    assert state["status"] == STATUS_UPLOADED
    assert state["attempts"] == 1
    assert state["server_capture_id"] == "srv_abc"
    assert state["next_attempt_at"] is None
    assert state["last_error_kind"] is None


async def test_live_upload_transient_schedules_retry(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec2", env.clock())
    env.upload.enqueue("transient_network")
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.TRANSIENT
    state = _read_upload_state(rec_dir)
    assert state["status"] == STATUS_FAILED_TRANSIENT
    assert state["attempts"] == 1
    next_at = datetime.fromisoformat(state["next_attempt_at"])
    delta = (next_at - env.clock()).total_seconds()
    # First tier is 300s ±10%.
    assert 270 <= delta <= 330


async def test_live_upload_permanent_client_is_terminal(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec3", env.clock())
    env.upload.enqueue("permanent_client", status=400)
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.PERMANENT_CLIENT
    state = _read_upload_state(rec_dir)
    assert state["status"] == STATUS_FAILED_PERMANENT


# ================================================================
# Credit-limit (402) handling
# ================================================================


async def test_credit_limit_sets_pause_and_notifies_once(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec4", env.clock())
    env.upload.enqueue("credit_limit")
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.CREDIT_LIMIT

    # Record is credit_blocked, not terminal.
    state = _read_upload_state(rec_dir)
    assert state["status"] == STATUS_CREDIT_BLOCKED
    assert state["next_attempt_at"] is None

    # Pause state persisted to disk.
    sidecar = env.captures_dir / env.cfg.pause_state_filename
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["credit_blocked_until"] is not None
    # Credit lockout ≈ 24h from now.
    expected_until = env.clock() + timedelta(seconds=env.cfg.credit_lockout_secs)
    got_until = datetime.fromisoformat(data["credit_blocked_until"])
    assert abs((got_until - expected_until).total_seconds()) < 2

    # Exactly one notification.
    assert len(env.notifier.calls) == 1
    title, body = env.notifier.calls[0]
    assert "pause" in title.lower() or "credit" in title.lower() or "Sayzo" in title
    assert "Sayzo" in body or "credit" in body.lower() or "actions" in body.lower()


async def test_two_consecutive_402s_notify_only_once(env):
    # First 402: live path for record A.
    a_dir, a_rec = _write_capture(env.captures_dir, "recA", env.clock())
    env.upload.enqueue("credit_limit")
    await env.mgr.try_upload(a_rec, a_dir)

    # Second capture comes in during the lockout. The live path should NOT
    # even call upload — should_attempt() returns False — and no new toast.
    b_dir, b_rec = _write_capture(env.captures_dir, "recB", env.clock() + timedelta(seconds=30))
    prior_upload_calls = len(env.upload.calls)
    outcome = await env.mgr.try_upload(b_rec, b_dir)
    assert outcome == UploadOutcome.CREDIT_LIMIT
    # Server NOT hit the second time.
    assert len(env.upload.calls) == prior_upload_calls
    # Record B marked credit_blocked without hitting the server.
    state = _read_upload_state(b_dir)
    assert state["status"] == STATUS_CREDIT_BLOCKED
    # Still only one toast total.
    assert len(env.notifier.calls) == 1


async def test_credit_lockout_expires_and_resumes(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec5", env.clock())
    env.upload.enqueue("credit_limit")
    await env.mgr.try_upload(record, rec_dir)
    # Sanity: blocked.
    assert not await env.mgr.should_attempt()

    # Advance past the 24h expiry.
    env.clock.advance(timedelta(seconds=env.cfg.credit_lockout_secs + 60))
    # Now should_attempt() returns True and clears the pause silently.
    assert await env.mgr.should_attempt() is True
    # No extra toast on resume.
    assert len(env.notifier.calls) == 1

    # A fresh upload goes through normally.
    env.upload.enqueue("success")
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.SUCCESS


# ================================================================
# Auth-required handling
# ================================================================


async def test_auth_required_sets_pause_and_notifies_once(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec6", env.clock())
    env.upload.enqueue("auth_required")
    outcome = await env.mgr.try_upload(record, rec_dir)
    assert outcome == UploadOutcome.AUTH_REQUIRED
    state = _read_upload_state(rec_dir)
    assert state["status"] == STATUS_AUTH_BLOCKED
    assert len(env.notifier.calls) == 1
    title, _ = env.notifier.calls[0]
    assert "sign-in" in title.lower() or "auth" in title.lower() or "Sayzo" in title


async def test_consecutive_auth_failures_notify_only_once(env):
    a_dir, a_rec = _write_capture(env.captures_dir, "recA2", env.clock())
    env.upload.enqueue("auth_required")
    await env.mgr.try_upload(a_rec, a_dir)

    b_dir, b_rec = _write_capture(env.captures_dir, "recB2", env.clock() + timedelta(seconds=10))
    await env.mgr.try_upload(b_rec, b_dir)
    assert len(env.notifier.calls) == 1


async def test_auth_recovery_clears_pause_silently(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec7", env.clock())
    env.upload.enqueue("auth_required")
    await env.mgr.try_upload(record, rec_dir)
    assert not await env.mgr.should_attempt()

    # Sweep triggers _check_auth_recovery. Make the probe succeed.
    env.auth_client.queue_token_result(True)
    await env.mgr.sweep_once()
    assert env.auth_client._invalidations >= 1
    # Pause cleared.
    assert await env.mgr.should_attempt() is True
    # No extra toast on recovery.
    assert len(env.notifier.calls) == 1


async def test_auth_recovery_still_failing_stays_blocked(env):
    rec_dir, record = _write_capture(env.captures_dir, "rec8", env.clock())
    env.upload.enqueue("auth_required")
    await env.mgr.try_upload(record, rec_dir)

    env.auth_client.queue_token_result(False)
    await env.mgr.sweep_once()
    assert not await env.mgr.should_attempt()


# ================================================================
# Sweep mechanics
# ================================================================


async def test_sweep_picks_up_due_records_oldest_first(env):
    # Three failed_transient records with next_attempt_at already in the past.
    # started_at ordered so "oldest" has the earliest started_at, then "middle",
    # then "newest". Oldest-first draining should hit them in that order.
    base = env.clock()
    past = (base - timedelta(seconds=1)).isoformat()
    # rec_id → minutes before base (larger = older)
    ages = [("oldest", 10), ("middle", 5), ("newest", 1)]
    for rec_id, minutes_old in ages:
        started = base - timedelta(minutes=minutes_old)
        state = dict(empty_upload_state())
        state["status"] = STATUS_FAILED_TRANSIENT
        state["attempts"] = 1
        state["next_attempt_at"] = past
        _write_capture(env.captures_dir, rec_id, started, upload_state=state)
    for _ in range(3):
        env.upload.enqueue("success")
    await env.mgr.sweep_once()
    assert env.upload.calls == ["oldest", "middle", "newest"]


async def test_sweep_skips_records_not_yet_due(env):
    base = env.clock()
    far_future = (base + timedelta(hours=1)).isoformat()
    state = dict(empty_upload_state())
    state["status"] = STATUS_FAILED_TRANSIENT
    state["attempts"] = 2
    state["next_attempt_at"] = far_future
    _write_capture(env.captures_dir, "not_due", base, upload_state=state)
    await env.mgr.sweep_once()
    assert env.upload.calls == []


async def test_sweep_skips_terminal_records(env):
    base = env.clock()
    for rec_id, status in (("done", STATUS_UPLOADED), ("dead", STATUS_FAILED_PERMANENT)):
        state = dict(empty_upload_state())
        state["status"] = status
        _write_capture(env.captures_dir, rec_id, base, upload_state=state)
    await env.mgr.sweep_once()
    assert env.upload.calls == []


async def test_sweep_honors_credit_pause(env):
    # Seed a credit lockout directly.
    await env.mgr._ensure_pause_state_loaded()
    env.mgr._pause_state.credit_blocked_until = env.clock() + timedelta(hours=12)
    await env.mgr._persist_pause_state()

    # Drop a record that would otherwise be due.
    state = dict(empty_upload_state())
    state["status"] = STATUS_PENDING
    _write_capture(env.captures_dir, "pending1", env.clock(), upload_state=state)

    await env.mgr.sweep_once()
    assert env.upload.calls == []


async def test_sweep_bails_mid_run_on_fresh_credit_hit(env):
    base = env.clock()
    # Four due records. First success, second 402 (sweep should stop).
    for i in range(4):
        state = dict(empty_upload_state())
        state["status"] = STATUS_PENDING
        _write_capture(env.captures_dir, f"r{i:02d}", base + timedelta(seconds=i), upload_state=state)
    env.upload.enqueue("success")
    env.upload.enqueue("credit_limit")
    # 3rd and 4th: set up but shouldn't be reached.
    env.upload.enqueue("success")
    env.upload.enqueue("success")

    await env.mgr.sweep_once()
    # Exactly two upload attempts — the 402 halts the sweep.
    assert len(env.upload.calls) == 2


async def test_sweep_respects_max_uploads_per_sweep(env):
    env.mgr._cfg.max_uploads_per_sweep = 2
    base = env.clock()
    for i in range(5):
        state = dict(empty_upload_state())
        state["status"] = STATUS_PENDING
        _write_capture(env.captures_dir, f"s{i:02d}", base + timedelta(seconds=i), upload_state=state)
    for _ in range(5):
        env.upload.enqueue("success")
    await env.mgr.sweep_once()
    assert len(env.upload.calls) == 2


async def test_sweep_skips_corrupt_record_json(env):
    # One good record, one broken record.json.
    good_dir, good_rec = _write_capture(env.captures_dir, "good1", env.clock())
    broken_dir = env.captures_dir / "broken1"
    broken_dir.mkdir()
    (broken_dir / "record.json").write_text("{not valid json", encoding="utf-8")
    (broken_dir / "audio.opus").write_bytes(b"x")

    env.upload.enqueue("success")
    await env.mgr.sweep_once()
    # Good record uploaded, broken one ignored.
    assert env.upload.calls == ["good1"]


async def test_sweep_picks_up_legacy_records_without_upload_state(env):
    # Simulate a record written before the upload-state feature shipped.
    legacy_dir, legacy_rec = _write_capture(
        env.captures_dir, "legacy1", env.clock(), upload_state="__legacy__"
    )
    assert "upload" not in legacy_rec.metadata
    env.upload.enqueue("success")
    await env.mgr.sweep_once()
    assert env.upload.calls == ["legacy1"]
    state = _read_upload_state(legacy_dir)
    assert state["status"] == STATUS_UPLOADED


# ================================================================
# Reconciliation of stuck in_flight records
# ================================================================


async def test_startup_sweep_reconciles_stuck_in_flight(env):
    # A record that a prior process crashed mid-upload on.
    base = env.clock()
    state = dict(empty_upload_state())
    state["status"] = STATUS_IN_FLIGHT
    state["attempts"] = 1
    state["last_attempt_at"] = (base - timedelta(hours=2)).isoformat()
    rec_dir, _ = _write_capture(env.captures_dir, "stuck1", base - timedelta(hours=2), upload_state=state)

    assert _read_upload_state(rec_dir)["status"] == STATUS_IN_FLIGHT

    await env.mgr.startup_sweep()

    # Reconcile flipped it to failed_transient with a 60s cooldown — the
    # immediate post-reconcile sweep doesn't attempt (next_attempt_at in
    # future).
    post = _read_upload_state(rec_dir)
    assert post["status"] == STATUS_FAILED_TRANSIENT
    assert env.upload.calls == []

    # After the cooldown elapses, the next sweep picks it up.
    env.clock.advance(timedelta(seconds=90))
    env.upload.enqueue("success")
    await env.mgr.sweep_once()
    post = _read_upload_state(rec_dir)
    assert post["status"] == STATUS_UPLOADED
    # attempts: 1 (pre-crash) + 1 (this attempt); reconcile does NOT bump.
    assert post["attempts"] == 2


# ================================================================
# Pause state persistence across restart
# ================================================================


async def test_pause_state_persists_across_restart(env):
    rec_dir, record = _write_capture(env.captures_dir, "persist1", env.clock())
    env.upload.enqueue("credit_limit")
    await env.mgr.try_upload(record, rec_dir)
    # Tear down this manager and create a fresh one pointing at the same dir.
    new_cfg = UploadConfig()
    new_upload = MockUploadClient()
    new_notifier = MockNotifier()
    mgr2 = UploadRetryManager(
        captures_dir=env.captures_dir,
        upload_client=new_upload,
        notifier=new_notifier,
        executor=env.executor,
        config=new_cfg,
        clock=env.clock,
    )
    # Fresh manager sees the lockout: should_attempt = False, sweep is a no-op.
    assert await mgr2.should_attempt() is False
    # Drop another capture; try_upload marks it credit_blocked without hitting
    # the server and without firing a second notification.
    b_dir, b_rec = _write_capture(env.captures_dir, "persist2", env.clock() + timedelta(seconds=10))
    outcome = await mgr2.try_upload(b_rec, b_dir)
    assert outcome == UploadOutcome.CREDIT_LIMIT
    assert new_upload.calls == []
    assert new_notifier.calls == []  # ← throttle survived the restart


# ================================================================
# In-flight collision
# ================================================================


async def test_in_flight_set_prevents_double_upload(env):
    rec_dir, record = _write_capture(env.captures_dir, "race1", env.clock())

    # Make the upload hang briefly so we can fire a second try_upload
    # concurrently while the first is still running.
    async def _slow_upload(r):
        await asyncio.sleep(0.2)
        return f"srv_{r.id}"

    env.upload.upload = _slow_upload  # type: ignore[assignment]

    task1 = asyncio.create_task(env.mgr.try_upload(record, rec_dir))
    await asyncio.sleep(0.01)
    task2 = asyncio.create_task(env.mgr.try_upload(record, rec_dir))
    outcomes = await asyncio.gather(task1, task2)
    # One succeeded, the other was skipped (returns None for "already in flight").
    assert UploadOutcome.SUCCESS in outcomes
    assert None in outcomes


# ================================================================
# Corrupt sidecar
# ================================================================


async def test_corrupt_pause_state_sidecar_recovers(env):
    # Plant garbage at the sidecar path BEFORE the manager loads it.
    sidecar = env.captures_dir / env.cfg.pause_state_filename
    sidecar.write_text("this is not json", encoding="utf-8")

    # Should not crash; manager starts with empty pause state.
    assert await env.mgr.should_attempt() is True
