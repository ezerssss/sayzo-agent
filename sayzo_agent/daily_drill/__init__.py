"""Daily-drill notification system.

Fires one well-timed native notification per workday that opens the day's
pre-generated 60-second speaking drill in the user's default browser.

Timing is learned per-user via a (day_of_week, hour) bucket model with
Thompson-sampled hour selection, so the schedule shifts toward windows the
user actually engages with.

The arm subsystem and capture pipeline are not affected; this is a purely
additive subsystem spawned alongside them from ``Agent.run()``.

Lazy re-exports — importing the pure ``model`` for tests must not drag in
``httpx`` or other heavy deps.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import TodaySessionResponse, fetch_today_session
    from .copy import compose_copy
    from .model import (
        BucketModel,
        BucketStats,
        HistoryEntry,
        NotificationOutcome,
        NotificationStats,
    )
    from .scheduler import DailyDrillScheduler, FireResult

__all__ = [
    "BucketModel",
    "BucketStats",
    "DailyDrillScheduler",
    "FireResult",
    "HistoryEntry",
    "NotificationOutcome",
    "NotificationStats",
    "TodaySessionResponse",
    "compose_copy",
    "fetch_today_session",
]


def __getattr__(name: str):
    if name in {"BucketModel", "BucketStats", "HistoryEntry", "NotificationOutcome", "NotificationStats"}:
        from . import model
        return getattr(model, name)
    if name in {"TodaySessionResponse", "fetch_today_session"}:
        from . import api
        return getattr(api, name)
    if name == "compose_copy":
        from . import copy as _copy
        return _copy.compose_copy
    if name in {"DailyDrillScheduler", "FireResult"}:
        from . import scheduler
        return getattr(scheduler, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
