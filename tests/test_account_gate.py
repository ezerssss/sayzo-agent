"""Tests for sayzo_agent.account.gate.

Pure decision logic — no I/O, no async. Verifies each cached state maps
to the right allow/deny and that the kill switch + missing-cache paths
both default to "allow" (the documented forgiving behavior).
"""
from __future__ import annotations

import pytest

from sayzo_agent.account.cache import CachedAccountStatus
from sayzo_agent.account.gate import AccountGateDecision, decide_arm_gate


def _cached(state: str) -> CachedAccountStatus:
    return CachedAccountStatus(
        account_state=state,  # type: ignore[arg-type]
        onboarding_complete=state == "ok",
        onboarding_url="https://sayzo.app/onboarding",
        email=None,
        user_id=None,
        fetched_at="2026-05-04T12:00:00+00:00",
    )


def test_no_cache_allows() -> None:
    decision = decide_arm_gate(None)
    assert decision.allowed is True
    assert decision.reason is None


def test_ok_allows() -> None:
    decision = decide_arm_gate(_cached("ok"))
    assert decision.allowed is True


def test_onboarding_required_blocks_with_toast() -> None:
    decision = decide_arm_gate(_cached("onboarding_required"))
    assert decision.allowed is False
    assert decision.reason == "onboarding_required"
    assert decision.toast_title is not None
    assert "sayzo.app" in (decision.toast_body or "").lower()


def test_suspended_blocks_with_paused_copy() -> None:
    decision = decide_arm_gate(_cached("suspended"))
    assert decision.allowed is False
    assert decision.reason == "suspended"
    assert "paused" in (decision.toast_body or "").lower()


def test_deleted_blocks_with_removed_copy() -> None:
    decision = decide_arm_gate(_cached("deleted"))
    assert decision.allowed is False
    assert decision.reason == "deleted"
    assert "removed" in (decision.toast_body or "").lower()


def test_kill_switch_collapses_all_to_allow() -> None:
    """SAYZO_AUTH__ACCOUNT_CHECK_ENABLED=0 must override every blocked
    state — it's the rollback safety valve."""
    for state in ("ok", "onboarding_required", "suspended", "deleted"):
        decision = decide_arm_gate(_cached(state), enabled=False)
        assert decision.allowed is True, f"kill switch failed for {state}"


def test_unknown_future_state_allows_rather_than_blocks() -> None:
    """Defensive: an unrecognised cache value (future schema, etc.) must
    not lock the user out. Worst case for "allow" is one rejected upload;
    worst case for "block" is broken recording."""
    cached = CachedAccountStatus(
        account_state="some_future_state",  # type: ignore[arg-type]
        onboarding_complete=False,
        onboarding_url=None,
        email=None,
        user_id=None,
        fetched_at="2026-05-04T12:00:00+00:00",
    )
    decision = decide_arm_gate(cached)
    assert decision.allowed is True


def test_decision_dataclass_is_frozen() -> None:
    """AccountGateDecision is meant to be immutable so callers can't
    mutate the cached _ALLOWED singleton across calls."""
    d = AccountGateDecision(allowed=True)
    with pytest.raises(Exception):
        d.allowed = False  # type: ignore[misc]
