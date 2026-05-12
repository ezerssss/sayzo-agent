"""Tests for sayzo_agent.last_version — single-line atomic version-marker store.

Drives the "Sayzo updated to vX.Y.Z" post-upgrade toast: the agent reads this
file on boot, compares against ``__version__``, and fires the toast iff
``read_last_seen() != __version__`` AND the candidate is newer.
"""
from __future__ import annotations

from pathlib import Path

from sayzo_agent.last_version import (
    LAST_SEEN_FILENAME,
    read_last_seen,
    write_last_seen,
)
from sayzo_agent.update import is_newer


def test_first_ever_launch_returns_none(tmp_path: Path) -> None:
    # No file on disk — the "did we just upgrade?" check must resolve to None,
    # which means "first install, don't fire an upgrade toast".
    assert read_last_seen(tmp_path) is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    write_last_seen(tmp_path, "2.7.12")
    assert read_last_seen(tmp_path) == "2.7.12"


def test_write_creates_data_dir_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "agent"
    assert not nested.exists()
    write_last_seen(nested, "2.7.12")
    assert (nested / LAST_SEEN_FILENAME).is_file()
    assert read_last_seen(nested) == "2.7.12"


def test_write_overwrites_previous(tmp_path: Path) -> None:
    write_last_seen(tmp_path, "2.7.12")
    write_last_seen(tmp_path, "2.8.0")
    assert read_last_seen(tmp_path) == "2.8.0"


def test_write_strips_whitespace(tmp_path: Path) -> None:
    write_last_seen(tmp_path, "  2.7.12 \n")
    assert read_last_seen(tmp_path) == "2.7.12"


def test_empty_file_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / LAST_SEEN_FILENAME).write_text("", encoding="utf-8")
    assert read_last_seen(tmp_path) is None


def test_whitespace_only_file_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / LAST_SEEN_FILENAME).write_text("   \n\n", encoding="utf-8")
    assert read_last_seen(tmp_path) is None


def test_multi_line_file_returns_first_line(tmp_path: Path) -> None:
    # Hypothetical corruption: a previous write got two versions appended.
    # Take the first line rather than crashing — let the upstream is_newer
    # parser decide what to do with the value.
    (tmp_path / LAST_SEEN_FILENAME).write_text(
        "2.8.0\n2.7.12\n", encoding="utf-8"
    )
    assert read_last_seen(tmp_path) == "2.8.0"


def test_write_empty_version_is_noop(tmp_path: Path) -> None:
    # Defensive: callers passing "" by mistake (e.g. dev source-tree with no
    # dist-info) shouldn't blank out a previously-written version.
    write_last_seen(tmp_path, "2.7.12")
    write_last_seen(tmp_path, "")
    assert read_last_seen(tmp_path) == "2.7.12"


# ---------------------------------------------------------------------------
# Integration with is_newer (the toast-decision logic)
# ---------------------------------------------------------------------------


def test_upgrade_detection(tmp_path: Path) -> None:
    # Simulate an upgrade: previous launch was v2.7.12, this launch is v2.8.0.
    write_last_seen(tmp_path, "2.7.12")
    prior = read_last_seen(tmp_path)
    assert prior == "2.7.12"
    current = "2.8.0"
    # Toast fires iff prior is set AND current is strictly newer.
    assert prior is not None and is_newer(prior, current)


def test_sidegrade_no_toast(tmp_path: Path) -> None:
    # Same version on relaunch — no toast.
    write_last_seen(tmp_path, "2.7.12")
    prior = read_last_seen(tmp_path)
    assert prior == "2.7.12"
    current = "2.7.12"
    assert prior is not None and not is_newer(prior, current)


def test_rollback_no_toast(tmp_path: Path) -> None:
    # Dev rolled back from v2.8.0 to v2.7.12. is_newer fails-safe to False so
    # we don't fire a "Sayzo updated" toast on a downgrade.
    write_last_seen(tmp_path, "2.8.0")
    prior = read_last_seen(tmp_path)
    assert prior == "2.8.0"
    current = "2.7.12"
    assert prior is not None and not is_newer(prior, current)


def test_first_install_no_toast(tmp_path: Path) -> None:
    # First-ever launch: read_last_seen returns None, no toast.
    assert read_last_seen(tmp_path) is None
