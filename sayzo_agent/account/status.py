"""Authenticated GET to ``/api/me`` with a typed response.

Wraps the platform's account-status endpoint:

::

    GET https://sayzo.app/api/me
    Authorization: Bearer <user token>

    200 → { user_id, email, onboarding_complete, onboarding_url,
            account_state: "active" | "onboarding_required"
                         | "suspended" | "deleted",
            issued_at }
    401 → token invalid (transient; AuthenticatedClient refreshes once)
    404/410 → account deleted
    5xx / network → retry with exponential backoff (caller-defined max)

The function never raises — every branch maps to a ``status`` field on
``AccountStatusResponse``. The caller dispatches on that status (cache
write / gate decision / GUI re-route) and is the single place where
"allow / block" decisions live.

Mirrors the shape of :mod:`sayzo_agent.daily_drill.api` so the two
authenticated GETs share patterns (retry math, error mapping,
``AuthenticationRequired`` handling) and tests can use the same helpers.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

import httpx

from ..auth.exceptions import AuthenticationRequired

if TYPE_CHECKING:
    from ..auth.client import AuthenticatedClient

log = logging.getLogger(__name__)


_API_PATH = "/api/me"


ResponseStatus = Literal[
    "ok",  # account_state == "active"
    "onboarding_required",
    "suspended",
    "deleted",
    "auth_required",
    "transient_error",
    "unknown_error",
]


@dataclass
class AccountStatusResponse:
    """Typed view of ``GET /api/me``.

    Only ``status`` is always populated. The other fields are set when the
    server returned a usable payload (``ok`` / ``onboarding_required`` /
    ``suspended`` / ``deleted``) — failure cases leave them ``None``.
    """

    status: ResponseStatus
    onboarding_complete: Optional[bool] = None
    onboarding_url: Optional[str] = None
    email: Optional[str] = None
    user_id: Optional[str] = None

    @property
    def is_persistable(self) -> bool:
        """True if this response should be written to the on-disk cache.

        Fetch-failure states (auth_required / transient_error /
        unknown_error) are deliberately NOT persisted — the cache reflects
        the last observed account state, not the last attempt outcome. A
        transient network blip should never overwrite a positive cache.
        """
        return self.status in ("ok", "onboarding_required", "suspended", "deleted")

    @property
    def is_allowed(self) -> bool:
        """True if the agent is allowed to record under this status.

        Used by the arm-time gate. Fetch-failure states are also allowed
        here — the gate falls back to whatever the cache says (or "allow"
        if there's no cache yet); this property only describes what *this*
        response would imply if used directly.
        """
        return self.status == "ok"


async def fetch_account_status(
    client: "AuthenticatedClient",
    *,
    max_retries: int = 3,
    base_backoff_secs: float = 2.0,
    rng: Optional[random.Random] = None,
) -> AccountStatusResponse:
    """GET ``/api/me`` with retries on transient failures.

    ``max_retries`` is the total number of attempts (so 3 = 1 initial + 2
    retries). Backoff between attempts is
    ``base_backoff_secs * 2**attempt + jitter`` with ±10 % jitter, drawn
    from ``rng`` if supplied (defaults to a fresh ``random.Random()``).

    Never raises. Failures map to ``status="auth_required"`` /
    ``"transient_error"`` / ``"unknown_error"``.
    """
    rng = rng or random.Random()
    last_error: Optional[str] = None

    for attempt in range(max_retries):
        try:
            resp = await client.get(_API_PATH)
        except AuthenticationRequired:
            log.info(
                "[account.status] /me: not authenticated (attempt %d)", attempt + 1
            )
            return AccountStatusResponse(status="auth_required")
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            last_error = repr(exc)
            log.info(
                "[account.status] /me network/timeout (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                last_error,
            )
            if attempt + 1 < max_retries:
                await _sleep_backoff(base_backoff_secs, attempt, rng)
                continue
            return AccountStatusResponse(status="transient_error")
        except Exception as exc:
            last_error = repr(exc)
            log.warning(
                "[account.status] /me unexpected error: %s",
                last_error,
                exc_info=True,
            )
            return AccountStatusResponse(status="unknown_error")

        sc = resp.status_code

        if sc == 200:
            return _parse_200(resp)

        if sc == 401:
            log.info("[account.status] /me returned 401")
            return AccountStatusResponse(status="auth_required")

        if sc in (404, 410):
            # Server signals a deleted/missing account by status code rather
            # than payload — accept that path too. Backend may use either.
            log.info("[account.status] /me returned %d → deleted", sc)
            return AccountStatusResponse(status="deleted")

        if 500 <= sc < 600:
            last_error = f"HTTP {sc}"
            log.info(
                "[account.status] /me returned %d (attempt %d/%d)",
                sc,
                attempt + 1,
                max_retries,
            )
            if attempt + 1 < max_retries:
                await _sleep_backoff(base_backoff_secs, attempt, rng)
                continue
            return AccountStatusResponse(status="transient_error")

        # Other 4xx — log + give up.
        log.warning("[account.status] /me returned unexpected status %d", sc)
        return AccountStatusResponse(status="unknown_error")

    log.warning(
        "[account.status] /me retries exhausted: %s", last_error
    )
    return AccountStatusResponse(status="transient_error")


def _parse_200(resp: httpx.Response) -> AccountStatusResponse:
    try:
        payload = resp.json()
    except ValueError:
        log.warning("[account.status] /me returned 200 with non-JSON body")
        return AccountStatusResponse(status="unknown_error")
    if not isinstance(payload, dict):
        log.warning("[account.status] /me 200 payload is not an object")
        return AccountStatusResponse(status="unknown_error")

    server_state = _str_or_none(payload.get("account_state"))
    onboarding_complete = bool(payload.get("onboarding_complete", False))
    onboarding_url = _str_or_none(payload.get("onboarding_url"))
    email = _str_or_none(payload.get("email"))
    user_id = _str_or_none(payload.get("user_id"))

    status = _map_server_state(server_state, onboarding_complete)
    if status is None:
        log.warning(
            "[account.status] /me returned unknown account_state=%r", server_state
        )
        return AccountStatusResponse(status="unknown_error")

    return AccountStatusResponse(
        status=status,
        onboarding_complete=onboarding_complete,
        onboarding_url=onboarding_url,
        email=email,
        user_id=user_id,
    )


def _map_server_state(
    server_state: Optional[str], onboarding_complete: bool
) -> Optional[ResponseStatus]:
    """Map server's ``account_state`` to the agent's ``ResponseStatus``.

    The server uses ``"active"`` for the happy path; the agent uses ``"ok"``
    to be consistent with :mod:`sayzo_agent.daily_drill.api` (where ``ok``
    means "200 with usable data"). The other values pass through verbatim.

    Tolerates a server that omits ``account_state`` entirely by deriving
    from ``onboarding_complete`` — that way an early backend version that
    only ships the bool still works, with the same gating semantics.
    """
    if server_state is None:
        return "ok" if onboarding_complete else "onboarding_required"
    if server_state == "active":
        return "ok"
    if server_state in ("onboarding_required", "suspended", "deleted"):
        return server_state  # type: ignore[return-value]
    return None


async def _sleep_backoff(
    base: float, attempt: int, rng: random.Random
) -> None:
    """Sleep ``base * 2**attempt`` with ±10% jitter."""
    delay = base * (2**attempt)
    jitter = delay * 0.1 * (2 * rng.random() - 1)
    delay = max(0.0, delay + jitter)
    await asyncio.sleep(delay)


def _str_or_none(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


__all__ = [
    "AccountStatusResponse",
    "ResponseStatus",
    "fetch_account_status",
]
