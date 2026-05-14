"""Small helpers shared by all capture sources (mic + system loopback)."""
from __future__ import annotations

import asyncio


def drain_queue(queue: asyncio.Queue) -> None:
    """Drop every pending item from an asyncio queue, non-blocking.

    Called by capture sources on ``start()`` (clear stale frames left by a
    previous arm cycle) and ``stop()`` (clear in-flight frames the producer
    thread enqueued after the stop signal but before it actually exited).
    """
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
