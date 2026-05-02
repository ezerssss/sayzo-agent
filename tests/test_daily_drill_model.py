"""Tests for the daily-drill bucket model and persistence.

Pure logic — no I/O beyond ``tmp_path`` JSON round-trips, no httpx, no
asyncio. The Thompson-sampling test uses a fixed-seed ``random.Random``
to assert deterministic preference for the favored hour.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sayzo_agent.config import NotificationConfig
from sayzo_agent.daily_drill.model import (
    BucketModel,
    BucketStats,
    NotificationStats,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NotificationConfig:
    base = dict(
        cold_start_hour=11,
        min_hour=9,
        lunch_start_hour=12,
        lunch_end_hour=13,
        max_hour=17,
        prior_alpha=3.0,
        recency_decay=0.95,
        bad_score_threshold=0.05,
        max_history=200,
    )
    base.update(overrides)
    return NotificationConfig(**base)


def _model(**overrides) -> BucketModel:
    return BucketModel(NotificationStats(), _cfg(**overrides))


def _dt(year=2026, month=5, day=4, hour=10, minute=0) -> datetime:
    """Default = Monday 2026-05-04 10:00 local."""
    return datetime(year, month, day, hour, minute)


# ---------------------------------------------------------------------------
# Cold-start slot pick
# ---------------------------------------------------------------------------


def test_cold_start_picks_eleven_am_when_in_window() -> None:
    m = _model()
    pick = m.pick_hour_today(as_of=_dt(hour=10), rng=random.Random(0))
    assert pick == 11


def test_cold_start_returns_now_hour_when_already_past_eleven() -> None:
    m = _model()
    pick = m.pick_hour_today(as_of=_dt(hour=14), rng=random.Random(0))
    assert pick == 14


def test_cold_start_skips_lunch_to_thirteen() -> None:
    m = _model()
    # 12:00 → cold-start target = max(12, 11) = 12 → in lunch → 13.
    pick = m.pick_hour_today(as_of=_dt(hour=12), rng=random.Random(0))
    assert pick == 13


def test_cold_start_returns_none_after_max_hour() -> None:
    m = _model()
    pick = m.pick_hour_today(as_of=_dt(hour=17), rng=random.Random(0))
    assert pick is None


def test_cold_start_returns_eleven_when_called_before_min_hour() -> None:
    m = _model()
    # Caller is responsible for the time-of-day gate; if it asks at 7am,
    # cold start still nominates 11 as the target.
    pick = m.pick_hour_today(as_of=_dt(hour=7), rng=random.Random(0))
    assert pick == 11


# ---------------------------------------------------------------------------
# Thompson-sampled hour pick (with history)
# ---------------------------------------------------------------------------


def test_thompson_sampling_prefers_high_tap_bucket() -> None:
    """Seed Monday: 10am has 8 taps, 14:00 has 8 expires.

    With multiple untried alternative hours, Thompson sampling explores —
    the favored hour is the modal pick but won't dominate. Tighten the
    comparison by seeding all candidate hours so untried buckets don't
    compete with their default Beta(α, α).
    """
    cfg = _cfg()
    m = BucketModel(NotificationStats(), cfg)
    favored_hour = 10
    bad_hour = 14
    fired_at = _dt(hour=favored_hour)
    bad_fired_at = _dt(hour=bad_hour)
    for _ in range(8):
        m.record_fire(fired_at)
        m.record_outcome(fired_at, "tap", latency_ms=20_000, session_id=None)
    for _ in range(8):
        m.record_fire(bad_fired_at)
        m.record_outcome(bad_fired_at, "expire", latency_ms=None, session_id=None)
    # Seed every other eligible hour with one expire so untried hours don't
    # dominate via their default Beta(α, α) ≈ 0.5 mean.
    for h in (9, 11, 13, 15, 16):
        seed_at = _dt(hour=h)
        m.record_fire(seed_at)
        m.record_outcome(seed_at, "expire", latency_ms=None, session_id=None)

    favored_wins = 0
    bad_wins = 0
    iterations = 300
    for seed in range(iterations):
        pick = m.pick_hour_today(as_of=_dt(hour=9), rng=random.Random(seed))
        if pick == favored_hour:
            favored_wins += 1
        if pick == bad_hour:
            bad_wins += 1
    # The favored hour should be the modal pick by a wide margin AND
    # the bad hour should be excluded entirely once it crosses 3 fires
    # at score < threshold — so 0 wins.
    assert favored_wins / iterations >= 0.70, f"favored_wins={favored_wins}/{iterations}"
    assert bad_wins == 0, f"bad_wins={bad_wins}/{iterations}"


def test_two_bad_fires_does_not_exclude_bucket() -> None:
    """Below 3 fires the bad-score filter doesn't fire — bucket stays.

    Probabilistic check: across many seeds, the bad bucket is picked
    SOMETIMES. After a third fire it should be picked NEVER.
    """
    cfg = _cfg()
    m = BucketModel(NotificationStats(), cfg)
    bad_hour = 10
    fired_at = _dt(hour=bad_hour)
    for _ in range(2):
        m.record_fire(fired_at)
        m.record_outcome(fired_at, "expire", latency_ms=None, session_id=None)

    picks = [
        m.pick_hour_today(as_of=_dt(hour=bad_hour), rng=random.Random(seed))
        for seed in range(200)
    ]
    bad_picks_two = sum(1 for p in picks if p == bad_hour)
    assert bad_picks_two > 0, "two-fire bad bucket should not be excluded"

    # Third bad fire crosses the threshold — bucket excluded entirely.
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "expire", latency_ms=None, session_id=None)
    picks_after = [
        m.pick_hour_today(as_of=_dt(hour=bad_hour), rng=random.Random(seed))
        for seed in range(200)
    ]
    bad_picks_three = sum(1 for p in picks_after if p == bad_hour)
    assert bad_picks_three == 0, "three-fire bad bucket should be excluded"


def test_lunch_hour_excluded_from_candidates() -> None:
    """Once we're past cold-start (any fire recorded), lunch must be skipped."""
    m = _model()
    # Seed a single fire so we exit cold-start.
    fired_at = _dt(hour=10)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=10_000, session_id=None)

    # At 12:00 with no other engagement history, the only eligible window is
    # 13–16. Pick must be in [13, 16] never 12.
    for seed in range(20):
        pick = m.pick_hour_today(as_of=_dt(hour=12), rng=random.Random(seed))
        assert pick is not None and pick != 12 and 13 <= pick < 17


def test_outside_workday_window_returns_none() -> None:
    m = _model()
    fired_at = _dt(hour=10)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=10_000, session_id=None)
    pick = m.pick_hour_today(as_of=_dt(hour=17), rng=random.Random(0))
    assert pick is None


# ---------------------------------------------------------------------------
# Recency decay
# ---------------------------------------------------------------------------


def test_recency_decay_drops_score_after_fourteen_days() -> None:
    m = _model()
    fired_at = _dt(year=2026, month=4, day=20, hour=10)  # Monday
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=10_000, session_id=None)

    fresh = m.smoothed_score(0, 10, as_of=fired_at)
    decayed = m.smoothed_score(0, 10, as_of=fired_at + timedelta(days=14))
    # 0.95 ** 14 ≈ 0.488
    assert fresh > decayed > 0
    assert decayed == pytest.approx(fresh * (0.95 ** 14), abs=1e-6)


def test_recency_decay_zero_days_is_no_op() -> None:
    m = _model()
    fired_at = _dt(hour=10)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=10_000, session_id=None)
    score = m.smoothed_score(0, 10, as_of=fired_at)
    # weighted=1, decay=1, denom=4 → 0.25
    assert score == pytest.approx(1.0 / 4.0)


def test_smoothed_score_zero_for_absent_bucket() -> None:
    m = _model()
    assert m.smoothed_score(0, 10, as_of=_dt()) == 0.0


# ---------------------------------------------------------------------------
# Outcome ingest + history cap
# ---------------------------------------------------------------------------


def test_record_fire_then_outcome_bumps_correct_bucket() -> None:
    m = _model()
    fired_at = _dt(year=2026, month=5, day=6, hour=15)  # Wednesday
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=22_000, session_id="abc")
    b = m.stats.buckets["2-15"]
    assert b.fires == 1
    assert b.taps == 1
    assert b.soft_taps == 0
    assert b.expires == 0
    assert b.last_fired_at == fired_at.isoformat()
    assert len(m.stats.history) == 1
    assert m.stats.history[0].outcome == "tap"
    assert m.stats.history[0].session_id == "abc"
    assert m.stats.history[0].latency_ms == 22_000


def test_record_outcome_supports_each_outcome_type() -> None:
    m = _model()
    fired_at = _dt(hour=10)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=1_000, session_id=None)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "soft_tap", latency_ms=600_000, session_id=None)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "expire", latency_ms=None, session_id=None)
    b = m.stats.buckets["0-10"]
    assert (b.fires, b.taps, b.soft_taps, b.expires) == (3, 1, 1, 1)


def test_history_capped_drops_oldest() -> None:
    m = _model(max_history=5)
    fired_at = _dt(hour=10)
    for i in range(7):
        m.record_fire(fired_at + timedelta(seconds=i))
        m.record_outcome(
            fired_at + timedelta(seconds=i),
            "tap",
            latency_ms=i,
            session_id=str(i),
        )
    assert len(m.stats.history) == 5
    # Oldest two (latency 0, 1) dropped; newest five (2..6) preserved.
    assert [e.latency_ms for e in m.stats.history] == [2, 3, 4, 5, 6]
    # Bucket aggregate still counts all 7.
    assert m.stats.buckets["0-10"].fires == 7
    assert m.stats.buckets["0-10"].taps == 7


# ---------------------------------------------------------------------------
# Weekend gate helper
# ---------------------------------------------------------------------------


def test_has_any_weekend_engagement_false_for_weekday_only() -> None:
    m = _model()
    fired_at = _dt(hour=10)  # Monday
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=1_000, session_id=None)
    assert not m.has_any_weekend_engagement()


def test_has_any_weekend_engagement_true_after_saturday_tap() -> None:
    m = _model()
    sat = _dt(year=2026, month=5, day=9, hour=10)  # Saturday
    m.record_fire(sat)
    m.record_outcome(sat, "tap", latency_ms=1_000, session_id=None)
    assert m.has_any_weekend_engagement()


def test_has_any_weekend_engagement_false_for_weekend_expire_only() -> None:
    m = _model()
    sun = _dt(year=2026, month=5, day=10, hour=10)  # Sunday
    m.record_fire(sun)
    m.record_outcome(sun, "expire", latency_ms=None, session_id=None)
    assert not m.has_any_weekend_engagement()


def test_has_any_weekend_engagement_true_after_sunday_soft_tap() -> None:
    m = _model()
    sun = _dt(year=2026, month=5, day=10, hour=10)
    m.record_fire(sun)
    m.record_outcome(sun, "soft_tap", latency_ms=600_000, session_id=None)
    assert m.has_any_weekend_engagement()


# ---------------------------------------------------------------------------
# Load / save round-trip
# ---------------------------------------------------------------------------


def test_load_missing_returns_fresh_model(tmp_path: Path) -> None:
    cfg = _cfg()
    m = BucketModel.load(tmp_path / "stats.json", cfg)
    assert m.total_fires() == 0
    assert m.stats.last_fired_on_day is None


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    cfg = _cfg()
    m = BucketModel(NotificationStats(), cfg)
    fired_at = _dt(hour=10)
    m.record_fire(fired_at)
    m.record_outcome(fired_at, "tap", latency_ms=22_000, session_id="abc")
    m.stats.last_fired_on_day = "2026-05-04"
    m.stats.os_disabled_prompt_shown = True

    path = tmp_path / "stats.json"
    m.save_atomic(path)

    loaded = BucketModel.load(path, cfg)
    assert loaded.stats.last_fired_on_day == "2026-05-04"
    assert loaded.stats.os_disabled_prompt_shown is True
    b = loaded.stats.buckets["0-10"]
    assert b.fires == 1
    assert b.taps == 1
    assert b.last_fired_at == fired_at.isoformat()
    assert len(loaded.stats.history) == 1
    assert loaded.stats.history[0].session_id == "abc"


def test_load_handles_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("{not valid json")
    m = BucketModel.load(path, _cfg())
    assert m.total_fires() == 0


def test_load_handles_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("[1, 2, 3]")
    m = BucketModel.load(path, _cfg())
    assert m.total_fires() == 0


def test_load_handles_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"version": 999, "buckets": {"0-10": {"fires": 5}}}))
    m = BucketModel.load(path, _cfg())
    assert m.total_fires() == 0  # forward-compat: treat as fresh


def test_save_atomic_is_atomic_replace(tmp_path: Path) -> None:
    """No half-written .json.tmp file lingers after save."""
    cfg = _cfg()
    m = BucketModel(NotificationStats(), cfg)
    path = tmp_path / "stats.json"
    m.save_atomic(path)
    assert path.exists()
    assert not (tmp_path / "stats.json.tmp").exists()


def test_load_skips_history_entries_with_unknown_outcome(tmp_path: Path) -> None:
    """Forward-compat: a future build might add new outcome types. Don't
    crash on read; just drop the unknown rows."""
    path = tmp_path / "stats.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "buckets": {},
                "history": [
                    {
                        "fired_at": "2026-05-04T10:00:00",
                        "outcome": "future_outcome",
                        "latency_ms": 100,
                    },
                    {
                        "fired_at": "2026-05-04T11:00:00",
                        "outcome": "tap",
                        "latency_ms": 1000,
                    },
                ],
            }
        )
    )
    m = BucketModel.load(path, _cfg())
    assert len(m.stats.history) == 1
    assert m.stats.history[0].outcome == "tap"
