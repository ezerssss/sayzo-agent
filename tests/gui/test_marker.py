"""Unit tests for sayzo_agent.gui.setup.marker."""
from __future__ import annotations

from pathlib import Path

import pytest

from sayzo_agent.config import Config
from sayzo_agent.gui.setup.marker import (
    _MARKER_NAME,
    is_first_launch,
    mark_setup_seen,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_first_launch_true_when_marker_absent(cfg: Config) -> None:
    assert is_first_launch(cfg) is True


def test_first_launch_false_after_mark(cfg: Config) -> None:
    mark_setup_seen(cfg)
    assert is_first_launch(cfg) is False


def test_mark_is_idempotent(cfg: Config) -> None:
    mark_setup_seen(cfg)
    mark_setup_seen(cfg)
    assert (cfg.data_dir / _MARKER_NAME).exists()


def test_marker_lives_under_data_dir(cfg: Config) -> None:
    mark_setup_seen(cfg)
    assert (cfg.data_dir / _MARKER_NAME).is_file()
