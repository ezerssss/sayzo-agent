"""Tests for the atomic pidfile single-instance gate.

Focus: ``try_acquire_pidfile`` must be a true mutex. Two near-simultaneous
calls must result in exactly one returning True and the other False.
That's the property the user reported regressing in v2.1.17 ("there
shouldnt be more than one instance bruh") — the legacy ``is_running``
+ ``write_pid`` two-step had a TOCTOU window.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from sayzo_agent import pidfile


def test_acquire_on_empty_dir_succeeds(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    assert pidfile.try_acquire_pidfile(pid_path) is True
    assert pid_path.exists()
    assert pid_path.read_text().strip() == str(os.getpid())


def test_acquire_when_alive_pid_already_holds_fails(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    # Write our own PID — definitely alive (we are us). Second acquire
    # must lose because the holder is alive.
    pid_path.write_text(str(os.getpid()))
    assert pidfile.try_acquire_pidfile(pid_path) is False


def test_acquire_replaces_stale_pidfile(tmp_path: Path) -> None:
    """A pidfile pointing at a long-dead PID is treated as stale."""
    pid_path = tmp_path / "agent.pid"
    # PID 999999 is well above any plausible live process (Linux default
    # max is 32768, Windows allocates lower numbers in practice). Both
    # ``os.kill(999999, 0)`` paths raise OSError, so is_running cleans
    # the pidfile and the retry succeeds.
    pid_path.write_text("999999")
    assert pidfile.try_acquire_pidfile(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_acquire_handles_corrupt_pidfile(tmp_path: Path) -> None:
    """A pidfile with non-numeric content is also treated as stale."""
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("not-a-pid\n")
    assert pidfile.try_acquire_pidfile(pid_path) is True


def test_concurrent_acquires_have_exactly_one_winner(tmp_path: Path) -> None:
    """The whole point of O_EXCL: a parallel race has one winner."""
    pid_path = tmp_path / "agent.pid"
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        # All threads block on the barrier so they all hit O_EXCL at
        # essentially the same instant.
        barrier.wait()
        results.append(pidfile.try_acquire_pidfile(pid_path))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads share the same PID (they're in the same Python
    # process), so the loser branch's "is alive?" check sees a
    # live PID and refuses. Exactly one O_EXCL winner; everyone
    # else returns False.
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_remove_pid_is_idempotent(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    pidfile.remove_pid(pid_path)  # missing — no-op
    pidfile.try_acquire_pidfile(pid_path)
    pidfile.remove_pid(pid_path)
    pidfile.remove_pid(pid_path)  # gone — no-op
    assert not pid_path.exists()


def test_is_running_treats_cross_privilege_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the post-install-elevated vs. user-clicked scenario.

    When the post-install Sayzo agent runs elevated and a later
    user-clicked Sayzo (medium integrity) tries ``os.kill(elevated_pid,
    0)``, Windows refuses with ERROR_ACCESS_DENIED → ``PermissionError``.
    The old logic treated that as "process gone" and removed the pidfile,
    letting two instances coexist. ``is_running`` must report True for a
    PID that ``psutil.pid_exists`` confirms is alive — even if our own
    ``os.kill`` probe would have failed.
    """
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("12345")  # arbitrary "elevated" PID

    import psutil
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 12345)

    # Pretend os.kill would fail with PermissionError if it were tried.
    # The is_running fast-path uses psutil first, so the os.kill mock is
    # belt-and-braces: even if the psutil branch were skipped, the
    # PermissionError-as-alive branch should catch it.
    def _fake_kill(pid: int, sig: int) -> None:
        raise PermissionError("Access is denied")

    monkeypatch.setattr(pidfile.os, "kill", _fake_kill)

    assert pidfile.is_running(pid_path) is True
    # Pidfile must NOT be removed.
    assert pid_path.exists()


def test_acquire_loses_to_cross_privilege_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: try_acquire_pidfile must lose to a cross-privilege primary."""
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("12345")

    import psutil
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == 12345)

    assert pidfile.try_acquire_pidfile(pid_path) is False
    assert pid_path.read_text().strip() == "12345"  # primary's PID intact
