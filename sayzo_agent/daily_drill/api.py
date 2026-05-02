"""Authenticated GET to ``/api/sessions/today`` with a typed response.

Wraps the platform's already-shipped endpoint:

::

    GET https://sayzo.app/api/sessions/today
    Authorization: Bearer <user token>

    200 → { sessionId, deepLinkUrl, isReplay, scenarioTitle, question }
    402 → user is over credit limit; do not fire today
    409 → DRILL_RETRY_REQUIRED or DRILL_STILL_PROCESSING; can fire but
          link points at the existing drill (the platform will recover)
    401 → token invalid (transient; the next reauth refreshes)
    5xx / network → retry with exponential backoff (caller-defined max)

The function never raises — every branch maps to a ``status`` field on
``TodaySessionResponse``. The scheduler dispatches on that status and is
the single place where "fire / skip / mark-done" decisions live.

Authentication uses the existing ``AuthenticatedClient`` (auth/client.py)
which auto-refreshes on 401. ``AuthenticationRequired`` from the auth
layer surfaces here as ``status="auth_required"``.
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


_API_PATH = "/api/sessions/today"


ResponseStatus = Literal[
    "ok",
    "over_credit",
    "still_processing",
    "retry_required",
    "auth_required",
    "transient_error",
    "unknown_error",
]


@dataclass
class TodaySessionResponse:
    """Typed view of ``GET /api/sessions/today``.

    Only ``status`` is always populated. ``deep_link_url`` and friends are
    set on success / 409 (existing-drill re-fire) paths.
    """

    status: ResponseStatus
    session_id: Optional[str] = None
    deep_link_url: Optional[str] = None
    is_replay: bool = False
    scenario_title: Optional[str] = None
    question: Optional[str] = None

    @property
    def fireable(self) -> bool:
        """True if the scheduler should send a notification using this response.

        Includes the 409 paths because the spec says we can still fire — the
        deep link points at the existing in-progress drill, and the platform
        recovers from there.
        """
        return self.status in ("ok", "still_processing", "retry_required")


async def fetch_today_session(
    client: "AuthenticatedClient",
    *,
    max_retries: int = 3,
    base_backoff_secs: float = 2.0,
    rng: Optional[random.Random] = None,
) -> TodaySessionResponse:
    """GET ``/api/sessions/today`` with retries on transient failures.

    ``max_retries`` is the total number of attempts (so 3 = 1 initial + 2
    retries). Backoff between attempts is
    ``base_backoff_secs * 2**attempt + jitter`` with ±10 % jitter, drawn
    from ``rng`` if supplied (defaults to a fresh ``random.Random()``).

    Never raises. Failures map to ``status="transient_error"`` or
    ``"unknown_error"``; the scheduler treats both as "silent skip today."
    """
    rng = rng or random.Random()
    last_error: Optional[str] = None

    for attempt in range(max_retries):
        try:
            resp = await client.get(_API_PATH)
        except AuthenticationRequired:
            log.info(
                "[daily_drill.api] /sessions/today: not authenticated (attempt %d)",
                attempt + 1,
            )
            return TodaySessionResponse(status="auth_required")
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            last_error = repr(exc)
            log.info(
                "[daily_drill.api] /sessions/today network/timeout (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                last_error,
            )
            if attempt + 1 < max_retries:
                await _sleep_backoff(base_backoff_secs, attempt, rng)
                continue
            return TodaySessionResponse(status="transient_error")
        except Exception as exc:
            last_error = repr(exc)
            log.warning(
                "[daily_drill.api] /sessions/today unexpected error: %s",
                last_error,
                exc_info=True,
            )
            return TodaySessionResponse(status="unknown_error")

        # Got a response — branch on status.
        sc = resp.status_code

        if sc == 200:
            try:
                payload = resp.json()
            except ValueError:
                log.warning(
                    "[daily_drill.api] /sessions/today returned 200 with non-JSON body"
                )
                return TodaySessionResponse(status="unknown_error")
            if not isinstance(payload, dict):
                log.warning(
                    "[daily_drill.api] /sessions/today 200 payload is not an object"
                )
                return TodaySessionResponse(status="unknown_error")
            return TodaySessionResponse(
                status="ok",
                session_id=_str_or_none(payload.get("sessionId")),
                deep_link_url=_str_or_none(payload.get("deepLinkUrl")),
                is_replay=bool(payload.get("isReplay", False)),
                scenario_title=_str_or_none(payload.get("scenarioTitle")),
                question=_str_or_none(payload.get("question")),
            )

        if sc == 401:
            log.info("[daily_drill.api] /sessions/today returned 401")
            return TodaySessionResponse(status="auth_required")

        if sc == 402:
            log.info("[daily_drill.api] /sessions/today returned 402 (over credit)")
            return TodaySessionResponse(status="over_credit")

        if sc == 409:
            # Spec: 409 may carry DRILL_STILL_PROCESSING or DRILL_RETRY_REQUIRED;
            # either way the platform expects us to fire — the link will point
            # at the existing in-progress drill.
            payload = _safe_json(resp)
            code = (payload or {}).get("code") if isinstance(payload, dict) else None
            mapped: ResponseStatus = (
                "retry_required" if code == "DRILL_RETRY_REQUIRED" else "still_processing"
            )
            log.info(
                "[daily_drill.api] /sessions/today returned 409 code=%r → status=%s",
                code,
                mapped,
            )
            payload_dict = payload if isinstance(payload, dict) else {}
            return TodaySessionResponse(
                status=mapped,
                session_id=_str_or_none(payload_dict.get("sessionId")),
                deep_link_url=_str_or_none(payload_dict.get("deepLinkUrl")),
                is_replay=bool(payload_dict.get("isReplay", False)),
                scenario_title=_str_or_none(payload_dict.get("scenarioTitle")),
                question=_str_or_none(payload_dict.get("question")),
            )

        if 500 <= sc < 600:
            last_error = f"HTTP {sc}"
            log.info(
                "[daily_drill.api] /sessions/today returned %d (attempt %d/%d)",
                sc,
                attempt + 1,
                max_retries,
            )
            if attempt + 1 < max_retries:
                await _sleep_backoff(base_backoff_secs, attempt, rng)
                continue
            return TodaySessionResponse(status="transient_error")

        # Other 4xx — log + give up for the day.
        log.warning(
            "[daily_drill.api] /sessions/today returned unexpected status %d", sc
        )
        return TodaySessionResponse(status="unknown_error")

    # Loop exhausted without a return path — shouldn't happen in practice.
    log.warning("[daily_drill.api] /sessions/today retries exhausted: %s", last_error)
    return TodaySessionResponse(status="transient_error")


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


def _safe_json(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return None


__all__ = ["TodaySessionResponse", "fetch_today_session"]
