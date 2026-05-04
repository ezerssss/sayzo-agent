"""Pure decision logic for the arm-time account gate.

Reads ``CachedAccountStatus`` (or ``None`` for missing-cache) and returns
an :class:`AccountGateDecision` the ArmController consults before flipping
into ARMED. Lives in :mod:`sayzo_agent.account` so the rule is testable
without standing up an ArmController.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .cache import CachedAccountStatus


@dataclass(frozen=True)
class AccountGateDecision:
    """Result of :func:`decide_arm_gate`.

    ``allowed`` is the only field the ArmController acts on; the rest are
    diagnostic + UX (the toast title/body when blocked, the canonical
    reason string for logs and the tray menu state).
    """

    allowed: bool
    reason: Optional[str] = None
    toast_title: Optional[str] = None
    toast_body: Optional[str] = None


_ALLOWED = AccountGateDecision(allowed=True)


def decide_arm_gate(
    cached: Optional[CachedAccountStatus],
    *,
    enabled: bool = True,
) -> AccountGateDecision:
    """Decide whether the agent may arm based on the cached account status.

    The arm-time gate is intentionally permissive on missing / unknown
    cache: a brand-new install or a corrupted cache file should not lock
    the user out. Real "no" decisions only fire when the cache definitively
    says the account isn't ready.

    ``enabled=False`` (kill switch) collapses to "allowed" regardless of
    cache contents — the safety valve for rolling back the gate without
    shipping a new agent.
    """
    if not enabled:
        return _ALLOWED
    if cached is None:
        # No cache yet — boot refresh will populate. Don't block.
        return _ALLOWED

    state = cached.account_state
    if state == "ok":
        return _ALLOWED
    if state == "onboarding_required":
        return AccountGateDecision(
            allowed=False,
            reason="onboarding_required",
            toast_title="Finish setup at sayzo.app",
            toast_body=(
                "Sayzo needs your account to be ready before it can record. "
                "Open sayzo.app to finish onboarding."
            ),
        )
    if state == "suspended":
        return AccountGateDecision(
            allowed=False,
            reason="suspended",
            toast_title="Sayzo account paused",
            toast_body=(
                "Your Sayzo account is paused. Visit sayzo.app to reactivate."
            ),
        )
    if state == "deleted":
        return AccountGateDecision(
            allowed=False,
            reason="deleted",
            toast_title="Sayzo account removed",
            toast_body=(
                "This account has been removed. Sign in with a different "
                "account or visit sayzo.app for help."
            ),
        )

    # Defensive: an unrecognised state from a future cache version.
    # Allow rather than block, since the worst case for "allow when we
    # shouldn't" is one rejected upload, vs "block when we shouldn't"
    # which silently breaks the user's recording.
    return _ALLOWED


__all__ = ["AccountGateDecision", "decide_arm_gate"]
