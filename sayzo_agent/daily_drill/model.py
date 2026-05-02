"""Bucket model for daily-drill notification timing.

Pure logic: no I/O dependencies beyond ``json`` + ``Path`` for load/save.
No httpx, no asyncio, no platform queries — everything is synchronous and
deterministic given an injected ``random.Random`` so the unit tests can pin
Thompson-sampled outcomes.

The model tracks acceptance per ``(day_of_week, hour)`` bucket. Each fire
records an outcome (``tap`` / ``soft_tap`` / ``expire``) which is folded
into a smoothed score; per-bucket recency decay is applied lazily on read
so we don't need a daily compaction job.

Outcome semantics (decided by user planning round):

* ``tap`` — user clicked the notification within ``dismiss_window_secs``
  (default 5 min). Strong positive signal, weight ``+1``.
* ``soft_tap`` — user clicked between ``dismiss_window_secs`` and
  ``soft_tap_window_secs`` (5 min – 4 h). Late tap, possibly the user
  finally remembered. Weight ``+0.3``. Also recorded for taps that come
  via the EOD tray fallback.
* ``expire`` — no click within ``dismiss_window_secs``. Weight ``+0``.
  We deliberately collapsed dismiss-vs-expire here because OS APIs can't
  reliably tell us whether the user actively dismissed or just ignored.

The Thompson-sampling hour pick uses ``Beta(taps + soft_taps + α,
expires + α)`` per candidate hour; an untried hour gets ``Beta(α, α)``
which is symmetric around 0.5 and therefore gets sampled before
tried-and-failed hours converge on a low engagement rate.
"""
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from ..config import NotificationConfig

log = logging.getLogger(__name__)


NotificationOutcome = Literal["tap", "soft_tap", "expire"]

_SCHEMA_VERSION = 1


def _bucket_key(dow: int, hour: int) -> str:
    return f"{dow}-{hour}"


@dataclass
class BucketStats:
    """Per-(day-of-week, hour) outcome aggregate."""

    fires: int = 0
    taps: int = 0
    soft_taps: int = 0
    expires: int = 0
    last_fired_at: Optional[str] = None  # ISO 8601 local

    def as_dict(self) -> dict[str, Any]:
        return {
            "fires": self.fires,
            "taps": self.taps,
            "soft_taps": self.soft_taps,
            "expires": self.expires,
            "last_fired_at": self.last_fired_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BucketStats":
        return cls(
            fires=int(d.get("fires", 0)),
            taps=int(d.get("taps", 0)),
            soft_taps=int(d.get("soft_taps", 0)),
            expires=int(d.get("expires", 0)),
            last_fired_at=d.get("last_fired_at") or None,
        )


@dataclass
class HistoryEntry:
    """One per-event record. Capped by NotificationConfig.max_history."""

    fired_at: str
    session_id: Optional[str]
    outcome: NotificationOutcome
    latency_ms: Optional[int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fired_at": self.fired_at,
            "session_id": self.session_id,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
        }


@dataclass
class NotificationStats:
    """Persisted document at ``data_dir/notification-stats.json``."""

    version: int = _SCHEMA_VERSION
    buckets: dict[str, BucketStats] = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)
    last_fired_on_day: Optional[str] = None  # YYYY-MM-DD local
    eod_fallback_shown_on: Optional[str] = None
    os_disabled_prompt_shown: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "buckets": {k: v.as_dict() for k, v in self.buckets.items()},
            "history": [h.as_dict() for h in self.history],
            "last_fired_on_day": self.last_fired_on_day,
            "eod_fallback_shown_on": self.eod_fallback_shown_on,
            "os_disabled_prompt_shown": self.os_disabled_prompt_shown,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NotificationStats":
        version = int(d.get("version", _SCHEMA_VERSION))
        if version != _SCHEMA_VERSION:
            log.warning(
                "[daily_drill.model] unknown schema version %s; treating as fresh",
                version,
            )
            return cls()
        raw_buckets = d.get("buckets", {})
        if not isinstance(raw_buckets, dict):
            raw_buckets = {}
        buckets = {
            str(k): BucketStats.from_dict(v if isinstance(v, dict) else {})
            for k, v in raw_buckets.items()
        }
        raw_history = d.get("history", [])
        if not isinstance(raw_history, list):
            raw_history = []
        history: list[HistoryEntry] = []
        for entry in raw_history:
            if not isinstance(entry, dict):
                continue
            outcome = entry.get("outcome")
            if outcome not in ("tap", "soft_tap", "expire"):
                continue
            history.append(
                HistoryEntry(
                    fired_at=str(entry.get("fired_at", "")),
                    session_id=entry.get("session_id") or None,
                    outcome=outcome,  # type: ignore[arg-type]
                    latency_ms=(
                        int(entry["latency_ms"])
                        if isinstance(entry.get("latency_ms"), (int, float))
                        else None
                    ),
                )
            )
        return cls(
            version=version,
            buckets=buckets,
            history=history,
            last_fired_on_day=d.get("last_fired_on_day") or None,
            eod_fallback_shown_on=d.get("eod_fallback_shown_on") or None,
            os_disabled_prompt_shown=bool(d.get("os_disabled_prompt_shown", False)),
        )


class BucketModel:
    """Acceptance model + persistence wrapper.

    Constructed via :meth:`load`. The bucket dict is mutated in place by
    :meth:`record_fire` / :meth:`record_outcome`; callers responsible for
    invoking :meth:`save_atomic` after mutations.
    """

    def __init__(self, stats: NotificationStats, cfg: "NotificationConfig") -> None:
        self.stats = stats
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path, cfg: "NotificationConfig") -> "BucketModel":
        """Read the stats JSON. Missing / corrupt → fresh model."""
        if not path.exists():
            return cls(NotificationStats(), cfg)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            log.warning(
                "[daily_drill.model] failed to read %s", path, exc_info=True
            )
            return cls(NotificationStats(), cfg)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "[daily_drill.model] malformed JSON in %s; starting fresh",
                path,
                exc_info=True,
            )
            return cls(NotificationStats(), cfg)
        if not isinstance(data, dict):
            log.warning(
                "[daily_drill.model] %s decoded to non-object; starting fresh",
                path,
            )
            return cls(NotificationStats(), cfg)
        return cls(NotificationStats.from_dict(data), cfg)

    def save_atomic(self, path: Path) -> None:
        """Atomic write (temp + os.replace) to survive a mid-write crash."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self.stats.as_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.warning(
                "[daily_drill.model] failed to write %s", path, exc_info=True
            )
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Aggregate queries
    # ------------------------------------------------------------------

    def total_fires(self) -> int:
        return sum(b.fires for b in self.stats.buckets.values())

    def has_any_weekend_engagement(self) -> bool:
        """True if any Sat/Sun bucket has a tap or soft_tap on record.

        Used by the scheduler's weekend-cold-start gate: skip weekend
        firing entirely until the user proves they're sometimes around
        on weekends.
        """
        for key, b in self.stats.buckets.items():
            if not key:
                continue
            try:
                dow = int(key.split("-", 1)[0])
            except ValueError:
                continue
            if dow >= 5 and (b.taps + b.soft_taps) > 0:
                return True
        return False

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def smoothed_score(self, dow: int, hour: int, *, as_of: datetime) -> float:
        """Recency-decayed engagement score in [0, 1].

        Score = (taps*1.0 + soft_taps*0.3 + expires*0.0) * decay
                / (fires + alpha)

        Decay is per-bucket, applied to the most-recent-fire timestamp.
        Returns ``0.0`` for an absent bucket.
        """
        b = self.stats.buckets.get(_bucket_key(dow, hour))
        if b is None:
            return 0.0
        decay = self._decay_factor(b, as_of=as_of)
        weighted = b.taps * 1.0 + b.soft_taps * 0.3 + b.expires * 0.0
        return (weighted * decay) / (b.fires + self._cfg.prior_alpha)

    def _decay_factor(self, b: BucketStats, *, as_of: datetime) -> float:
        if not b.last_fired_at:
            return 1.0
        try:
            last = datetime.fromisoformat(b.last_fired_at)
        except ValueError:
            return 1.0
        days = max(0, (as_of.date() - last.date()).days)
        return self._cfg.recency_decay**days

    # ------------------------------------------------------------------
    # Slot pick
    # ------------------------------------------------------------------

    def pick_hour_today(
        self,
        *,
        as_of: datetime,
        rng: random.Random,
    ) -> Optional[int]:
        """Pick the hour (local) at which to fire today, or None.

        Cold start (no fires anywhere yet): deterministically returns
        ``cold_start_hour`` if it's still in today's window, else the next
        eligible hour after now.

        Otherwise: build candidate hours from ``max(now, min_hour)`` to
        ``max_hour`` excluding lunch + low-engagement-with-evidence
        buckets, then Thompson-sample
        ``Beta(taps + soft_taps + α, expires + α)`` per candidate; the
        highest sampled probability wins.
        """
        cfg = self._cfg

        if self.total_fires() == 0:
            return self._cold_start_pick(as_of)

        candidates: list[tuple[int, BucketStats]] = []
        dow = as_of.weekday()
        start_hour = max(as_of.hour, cfg.min_hour)
        for h in range(start_hour, cfg.max_hour):
            if cfg.lunch_start_hour <= h < cfg.lunch_end_hour:
                continue
            b = self.stats.buckets.get(_bucket_key(dow, h)) or BucketStats()
            if (
                b.fires >= 3
                and self.smoothed_score(dow, h, as_of=as_of)
                < cfg.bad_score_threshold
            ):
                continue
            candidates.append((h, b))

        if not candidates:
            return None

        best_hour: Optional[int] = None
        best_sample = -1.0
        for h, b in candidates:
            wins = b.taps + b.soft_taps + cfg.prior_alpha
            losses = b.expires + cfg.prior_alpha
            sample = rng.betavariate(wins, losses)
            if sample > best_sample:
                best_sample, best_hour = sample, h
        return best_hour

    def _cold_start_pick(self, as_of: datetime) -> Optional[int]:
        cfg = self._cfg
        target = max(as_of.hour, cfg.cold_start_hour)
        for h in range(target, cfg.max_hour):
            if cfg.lunch_start_hour <= h < cfg.lunch_end_hour:
                continue
            if h < cfg.min_hour:
                continue
            return h
        return None

    # ------------------------------------------------------------------
    # Outcome ingest
    # ------------------------------------------------------------------

    def record_fire(self, fired_at: datetime) -> None:
        """Bump the bucket's ``fires`` count + ``last_fired_at`` timestamp.

        Called when the OS toast is dispatched, before we know the outcome.
        Keeps the smoothed-score denominator consistent with reality.
        """
        key = _bucket_key(fired_at.weekday(), fired_at.hour)
        b = self.stats.buckets.setdefault(key, BucketStats())
        b.fires += 1
        b.last_fired_at = fired_at.isoformat()

    def record_outcome(
        self,
        fired_at: datetime,
        outcome: NotificationOutcome,
        *,
        latency_ms: Optional[int],
        session_id: Optional[str],
    ) -> None:
        """Append an outcome to the bucket + history log.

        ``fires`` is NOT bumped here — that happened in ``record_fire``.
        This call only updates the outcome counters.
        """
        key = _bucket_key(fired_at.weekday(), fired_at.hour)
        b = self.stats.buckets.setdefault(key, BucketStats())
        if outcome == "tap":
            b.taps += 1
        elif outcome == "soft_tap":
            b.soft_taps += 1
        elif outcome == "expire":
            b.expires += 1
        # last_fired_at already set by record_fire; refresh anyway in case
        # an outcome arrives without a preceding record_fire (test paths).
        if not b.last_fired_at:
            b.last_fired_at = fired_at.isoformat()

        self.stats.history.append(
            HistoryEntry(
                fired_at=fired_at.isoformat(),
                session_id=session_id,
                outcome=outcome,
                latency_ms=latency_ms,
            )
        )
        if len(self.stats.history) > self._cfg.max_history:
            # Drop oldest. Bucket aggregates already absorbed every event,
            # so we lose only the per-event timeline — counts are intact.
            self.stats.history = self.stats.history[-self._cfg.max_history:]


__all__ = [
    "BucketModel",
    "BucketStats",
    "HistoryEntry",
    "NotificationOutcome",
    "NotificationStats",
]
