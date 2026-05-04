"""Tests for sayzo_agent.account.cache.

Pure-disk tests: write a payload, read it back, exercise the failure
modes the gate needs to be defensive against (corrupt JSON, version
mismatch, missing fields).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sayzo_agent.account.cache import (
    CACHE_FILENAME,
    CACHE_SCHEMA_VERSION,
    CachedAccountStatus,
    cache_path,
    clear_cache,
    now_iso,
    read_cache,
    write_cache,
)
from sayzo_agent.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _make_cached(account_state: str = "ok") -> CachedAccountStatus:
    return CachedAccountStatus(
        account_state=account_state,  # type: ignore[arg-type]
        onboarding_complete=account_state == "ok",
        onboarding_url="https://sayzo.app/onboarding",
        email="user@example.com",
        user_id="usr_abc",
        fetched_at=now_iso(),
    )


def test_read_returns_none_when_missing(cfg: Config) -> None:
    assert read_cache(cfg) is None


def test_write_then_read_roundtrip(cfg: Config) -> None:
    cached = _make_cached("ok")
    write_cache(cfg, cached)
    out = read_cache(cfg)
    assert out is not None
    assert out.account_state == "ok"
    assert out.onboarding_complete is True
    assert out.onboarding_url == "https://sayzo.app/onboarding"
    assert out.email == "user@example.com"
    assert out.user_id == "usr_abc"


def test_write_persists_version_field(cfg: Config) -> None:
    write_cache(cfg, _make_cached())
    raw = json.loads(cache_path(cfg).read_text(encoding="utf-8"))
    assert raw["version"] == CACHE_SCHEMA_VERSION


def test_corrupt_json_returns_none(cfg: Config) -> None:
    cache_path(cfg).write_text("definitely not json", encoding="utf-8")
    assert read_cache(cfg) is None


def test_non_object_top_level_returns_none(cfg: Config) -> None:
    cache_path(cfg).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_cache(cfg) is None


def test_unknown_account_state_returns_none(cfg: Config) -> None:
    cache_path(cfg).write_text(
        json.dumps(
            {
                "version": CACHE_SCHEMA_VERSION,
                "account_state": "totally_made_up",
                "onboarding_complete": False,
                "onboarding_url": None,
                "email": None,
                "user_id": None,
                "fetched_at": "2026-05-04T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    assert read_cache(cfg) is None


def test_missing_fetched_at_returns_none(cfg: Config) -> None:
    cache_path(cfg).write_text(
        json.dumps(
            {
                "version": CACHE_SCHEMA_VERSION,
                "account_state": "ok",
                "onboarding_complete": True,
                "onboarding_url": None,
                "email": None,
                "user_id": None,
            }
        ),
        encoding="utf-8",
    )
    assert read_cache(cfg) is None


def test_version_mismatch_forces_refresh(cfg: Config) -> None:
    cache_path(cfg).write_text(
        json.dumps(
            {
                "version": CACHE_SCHEMA_VERSION + 99,
                "account_state": "ok",
                "onboarding_complete": True,
                "onboarding_url": None,
                "email": None,
                "user_id": None,
                "fetched_at": "2026-05-04T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    # A future cache should not be trusted.
    assert read_cache(cfg) is None


def test_clear_removes_file(cfg: Config) -> None:
    write_cache(cfg, _make_cached())
    assert cache_path(cfg).exists()
    clear_cache(cfg)
    assert not cache_path(cfg).exists()
    # Idempotent.
    clear_cache(cfg)


def test_clear_when_missing_is_noop(cfg: Config) -> None:
    assert not cache_path(cfg).exists()
    clear_cache(cfg)  # must not raise


def test_write_is_atomic_no_tmp_file_left(cfg: Config) -> None:
    write_cache(cfg, _make_cached())
    leftover = list(cfg.data_dir.glob(".account_status.*.tmp"))
    assert leftover == []


def test_age_seconds_returns_positive_for_past_timestamp(cfg: Config) -> None:
    from datetime import datetime, timezone

    cached = CachedAccountStatus(
        account_state="ok",
        onboarding_complete=True,
        onboarding_url=None,
        email=None,
        user_id=None,
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    age = cached.age_seconds(
        now=datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    )
    assert age == pytest.approx(3600, abs=1)


def test_filename_constant_lives_under_data_dir(cfg: Config) -> None:
    assert cache_path(cfg) == cfg.data_dir / CACHE_FILENAME
