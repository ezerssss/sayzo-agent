"""Account-status client for the web-onboarding gate.

The agent will refuse to record until the server confirms the signed-in user
has completed onboarding at sayzo.app. The cache is the runtime source of
truth; the network call refreshes it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .cache import (
    BLOCKED_ACCOUNT_STATES,
    CachedAccountStatus,
    VALID_ACCOUNT_STATES,
    cache_path,
    clear_cache,
    now_iso,
    read_cache,
    write_cache,
)
from .gate import AccountGateDecision, decide_arm_gate
from .status import (
    AccountStatusResponse,
    ResponseStatus,
    fetch_account_status,
)

if TYPE_CHECKING:
    from ..auth.client import AuthenticatedClient
    from ..config import Config


async def refresh_and_cache(
    client: "AuthenticatedClient",
    cfg: "Config",
    *,
    max_retries: int = 3,
    base_backoff_secs: float = 2.0,
) -> AccountStatusResponse:
    """Fetch ``/api/me`` and persist the result if it's a real account state.

    Failure responses (auth_required / transient_error / unknown_error) are
    deliberately NOT written to the cache — they're transient and should
    never overwrite a positive cache from an earlier successful fetch.
    """
    # Piggyback the opt-out diagnostics inventory headers (version / OS /
    # install-id) onto the poll we already make. Returns {} when the user has
    # opted out, so this is a no-op for them. Lazy import keeps the account
    # module's import graph free of the upload/httpx chain at load time.
    from ..diagnostics import diagnostics_headers

    response = await fetch_account_status(
        client,
        max_retries=max_retries,
        base_backoff_secs=base_backoff_secs,
        extra_headers=diagnostics_headers(cfg),
    )
    if response.is_persistable:
        write_cache(
            cfg,
            CachedAccountStatus(
                # Safe cast — is_persistable filters to the four cached states.
                account_state=response.status,  # type: ignore[arg-type]
                onboarding_complete=bool(response.onboarding_complete),
                onboarding_url=response.onboarding_url,
                email=response.email,
                user_id=response.user_id,
                fetched_at=now_iso(),
            ),
        )
    return response


__all__ = [
    "AccountGateDecision",
    "AccountStatusResponse",
    "BLOCKED_ACCOUNT_STATES",
    "CachedAccountStatus",
    "ResponseStatus",
    "VALID_ACCOUNT_STATES",
    "cache_path",
    "clear_cache",
    "decide_arm_gate",
    "fetch_account_status",
    "now_iso",
    "read_cache",
    "refresh_and_cache",
    "write_cache",
]
