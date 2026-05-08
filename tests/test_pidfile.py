"""Tests for the kernel-locked single-instance gate.

Focus: ``try_acquire_pidfile`` is a real OS-level mutex.

The v2.7.0 and earlier ``.pid`` file approach failed in three modes
across two version bumps:

    v2.1.18 — TOCTOU between is_running and write_pid → two primaries
    v2.1.19 — cross-privilege os.kill PermissionError → two primaries
    v2.7.0  — post-reboot PID recycling → zero primaries (the user
              report that triggered the v2.7.1 rewrite to kernel locks)

The kernel-locked rewrite eliminates *all* of these failure modes
because the kernel owns the lock state and auto-releases on process
death — clean exit, kill, BSOD, reboot.

These tests exercise the real CreateMutexW / flock primitives via
in-process thread contention; the kernel treats threads in one process
as independent contenders on a named mutex / flock, which gives us
real cross-process semantics without subprocess management.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from sayzo_agent import pidfile


@pytest.fixture(autouse=True)
def _release_after_test(tmp_path: Path):
    """Release any locks held against this tmp_path on test teardown.

    Each test uses a unique tmp_path so cross-test contamination is
    impossible at the kernel level (Windows mutex names are derived
    from the path; flock fds are per-test). But within a test, leftover
    held handles in ``_held_locks`` would accumulate. Yield, then
    sweep.
    """
    yield
    for lock_path in list(tmp_path.rglob("*.pid")):
        pidfile.remove_pid(lock_path)


def test_acquire_on_empty_dir_succeeds(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    assert pidfile.try_acquire_pidfile(pid_path) is True
    assert pid_path.exists()
    assert pid_path.read_text().strip() == str(os.getpid())


def test_acquire_overwrites_stale_pidfile_from_previous_boot(tmp_path: Path) -> None:
    """The post-reboot bug: a .pid file from a previous boot session
    survives on disk pointing at a recycled PID. Under the v2.7.0
    pidfile-as-liveness scheme, ``psutil.pid_exists`` would falsely
    report the recycled PID alive and lock every Sayzo launch out.
    Under the kernel-lock scheme, the stale file is harmless — no one
    holds the kernel mutex / flock, so we acquire and overwrite the
    file with our PID.
    """
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("13492")  # the user's bug report PID

    assert pidfile.try_acquire_pidfile(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_acquire_handles_corrupt_pidfile(tmp_path: Path) -> None:
    """A garbled .pid file (truncated write, hex content, etc.) is
    overwritten on the next acquire — the kernel-lock layer doesn't
    care about file contents at all, only whether the lock is held.
    """
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("not-a-pid\n")
    assert pidfile.try_acquire_pidfile(pid_path) is True
    assert pid_path.read_text().strip() == str(os.getpid())


def test_remove_pid_is_idempotent(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    pidfile.remove_pid(pid_path)  # missing — no-op
    pidfile.try_acquire_pidfile(pid_path)
    pidfile.remove_pid(pid_path)
    pidfile.remove_pid(pid_path)  # gone — no-op
    assert not pid_path.exists()


def test_acquire_release_acquire_round_trip(tmp_path: Path) -> None:
    """Once a process releases, the next acquire must succeed.

    Verifies the kernel actually drops the lock on ``remove_pid``;
    forgetting ``CloseHandle`` on Windows or ``LOCK_UN`` + ``close`` on
    Unix would silently leak the lock to the next call.
    """
    pid_path = tmp_path / "agent.pid"
    assert pidfile.try_acquire_pidfile(pid_path) is True
    pidfile.remove_pid(pid_path)
    assert pidfile.try_acquire_pidfile(pid_path) is True


def test_concurrent_acquires_have_exactly_one_winner(tmp_path: Path) -> None:
    """Real OS-level contention: same path, many threads, exactly one
    becomes primary.

    Each thread calls into ``try_acquire_pidfile`` simultaneously. The
    Windows kernel mutex (named by path-hash) and POSIX flock both
    treat threads in the same process as independent contenders, so
    this exercises real cross-process semantics.
    """
    pid_path = tmp_path / "agent.pid"
    results: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        won = pidfile.try_acquire_pidfile(pid_path)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_is_running_returns_false_when_no_holder(tmp_path: Path) -> None:
    pid_path = tmp_path / "agent.pid"
    assert pidfile.is_running(pid_path) is False


def test_is_running_returns_false_for_stale_pidfile_no_holder(tmp_path: Path) -> None:
    """Stale .pid file from a previous session, no live holder — the
    post-reboot scenario in isolation. ``is_running`` must report False
    so callers (e.g. the first-run setup flow's "is Sayzo already
    running?" branch) take the launch path instead of the no-op path.
    """
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("13492")
    assert pidfile.is_running(pid_path) is False


def test_is_running_returns_true_while_held(tmp_path: Path) -> None:
    """Hold the lock in a worker thread and verify the main thread
    sees ``is_running == True``. The worker holds until released, so
    the main-thread probe sees the kernel lock as occupied.
    """
    pid_path = tmp_path / "agent.pid"
    acquired = threading.Event()
    release = threading.Event()

    def hold() -> None:
        assert pidfile.try_acquire_pidfile(pid_path) is True
        acquired.set()
        release.wait(timeout=10)
        pidfile.remove_pid(pid_path)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        acquired.wait(timeout=5)
        assert pidfile.is_running(pid_path) is True
    finally:
        release.set()
        worker.join(timeout=5)

    # After release, the lock is gone and is_running flips back.
    assert pidfile.is_running(pid_path) is False


def test_acquire_blocks_while_other_holds_then_succeeds_after_release(
    tmp_path: Path,
) -> None:
    """The full single-instance dance: while A holds, B can't acquire.
    Once A releases, B can.
    """
    pid_path = tmp_path / "agent.pid"
    a_acquired = threading.Event()
    a_release = threading.Event()
    b_results: list[bool] = []

    def hold_a() -> None:
        assert pidfile.try_acquire_pidfile(pid_path) is True
        a_acquired.set()
        a_release.wait(timeout=10)
        pidfile.remove_pid(pid_path)

    a = threading.Thread(target=hold_a)
    a.start()
    a_acquired.wait(timeout=5)

    # B tries while A still holds — must lose.
    b_results.append(pidfile.try_acquire_pidfile(pid_path))

    # Release A; B retries — must win.
    a_release.set()
    a.join(timeout=5)

    b_results.append(pidfile.try_acquire_pidfile(pid_path))
    pidfile.remove_pid(pid_path)

    assert b_results == [False, True]


def test_distinct_paths_have_independent_locks(tmp_path: Path) -> None:
    """Two different pidfile paths must not contend.

    Production has both ``agent.pid`` (the listening service) and
    ``settings.pid`` (the Settings GUI subprocess). Both delegate to
    the same primitive, so they must have isolated locks — otherwise
    opening Settings while the agent runs would (incorrectly) report
    "already running."
    """
    pid_a = tmp_path / "agent.pid"
    pid_b = tmp_path / "settings.pid"
    assert pidfile.try_acquire_pidfile(pid_a) is True
    assert pidfile.try_acquire_pidfile(pid_b) is True
    pidfile.remove_pid(pid_a)
    pidfile.remove_pid(pid_b)


def test_pidfile_content_is_overwritten_on_each_acquire(tmp_path: Path) -> None:
    """Even if the .pid file was left with wrong content, the next
    successful acquire writes our current PID. This is what makes the
    file a reliable "current primary's PID" sticky note for IPC
    routing and diagnostics.
    """
    pid_path = tmp_path / "agent.pid"
    pid_path.write_text("999999")
    pidfile.try_acquire_pidfile(pid_path)
    assert pid_path.read_text().strip() == str(os.getpid())
    pidfile.remove_pid(pid_path)


def test_write_pid_is_unlocked_back_compat(tmp_path: Path) -> None:
    """``write_pid`` is the bare informational write — no kernel lock,
    no idempotence checks. Kept for back-compat callers who enforce
    single-instance some other way.
    """
    pid_path = tmp_path / "agent.pid"
    pidfile.write_pid(pid_path)
    assert pid_path.read_text().strip() == str(os.getpid())
    # Still no kernel lock held — another acquirer can come in fresh.
    assert pidfile.is_running(pid_path) is False
