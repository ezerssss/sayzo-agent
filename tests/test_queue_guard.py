"""Tests for FrameQueueGuard — the drop-on-full capture enqueue guard.

Regression guard for the v3.20 QueueFull-storm fix: a full capture queue must
DROP frames (with a throttled count), never let ``put_nowait`` raise QueueFull
into asyncio's default handler (which logged a numpy-array traceback per dropped
frame — ~63k lines / 97% of one production log, and ~200 s of lost mic audio).
"""
from __future__ import annotations

import asyncio

from sayzo_agent.capture.queue_guard import FrameQueueGuard


async def test_put_succeeds_until_full_then_drops_without_raising():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    g = FrameQueueGuard(q, label="mic")

    assert g.put((0.0, "a")) is True
    assert g.put((0.1, "b")) is True
    # Queue is now full — these MUST drop (return False) and MUST NOT raise.
    assert g.put((0.2, "c")) is False
    assert g.put((0.3, "d")) is False

    assert g.dropped_total == 2
    assert q.qsize() == 2
    # The frames that made it in are the first two (FIFO), undamaged.
    assert q.get_nowait() == (0.0, "a")
    assert q.get_nowait() == (0.1, "b")


async def test_drops_resume_after_drain():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    g = FrameQueueGuard(q, label="system")
    assert g.put((0.0, "a")) is True
    assert g.put((0.1, "b")) is False  # full → drop
    q.get_nowait()                      # consumer catches up
    assert g.put((0.2, "c")) is True   # room again
    assert g.dropped_total == 1


async def test_drop_log_is_throttled(caplog):
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    g = FrameQueueGuard(q, label="mic", log_interval_secs=999.0)
    g.put((0.0, "a"))  # fills
    import logging
    with caplog.at_level(logging.WARNING, logger="sayzo_agent.capture.queue_guard"):
        for i in range(50):
            g.put((float(i), "x"))  # all dropped
    # 50 drops, but the throttle (999 s window) means only the FIRST emits a
    # log line — not 50.
    drop_logs = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert len(drop_logs) == 1
    assert g.dropped_total == 50
