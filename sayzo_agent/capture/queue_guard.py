"""Drop-on-full enqueue guard shared by the capture producers (mic + system).

A full capture queue means the consumer (``app._consume``) fell behind —
normally a transient event-loop stall (GC pause, a brief blocking call). The
correct response is to **drop** the frame, not to let ``asyncio.Queue.put_nowait``
raise ``QueueFull`` into asyncio's *default* exception handler.

Before this guard, the mic producer (and the Windows system producer) scheduled
a bare ``put_nowait`` via ``call_soon_threadsafe``; when the queue was full, the
default handler logged a 6-line traceback **including the full numpy frame repr**
for *every* dropped frame. In one production capture session a blocked event loop
(a synchronous ``audio-detect`` subprocess running on the loop) produced ~10,000
of these in 20 minutes — ~63k log lines / 97% of the file — and, more importantly,
those were ~200 s of the user's own microphone audio silently dropped.

This guard drops silently and emits a single **throttled** summary so the real
signal ("frames are being dropped, the consumer is behind") survives without the
flood, and a numpy array is never repr'd into the log.

Thread-safety: ``put`` calls ``asyncio.Queue.put_nowait``, which is NOT
thread-safe. It MUST run on the event-loop thread — producers on a non-loop
thread (PortAudio callback, WASAPI reader) must schedule it via
``loop.call_soon_threadsafe(guard.put, item)``; an on-loop async reader may call
``guard.put(item)`` directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class FrameQueueGuard:
    """Wraps an ``asyncio.Queue`` with drop-on-full + throttled drop logging."""

    def __init__(
        self,
        queue: "asyncio.Queue[Any]",
        *,
        label: str,
        log_interval_secs: float = 5.0,
    ) -> None:
        self._queue = queue
        self._label = label
        self._log_interval = log_interval_secs
        self._dropped_since_log = 0
        self._dropped_total = 0
        self._last_log_ts = 0.0

    def put(self, item: Any) -> bool:
        """Enqueue ``item``; on ``QueueFull`` drop it and count. Returns True
        if queued, False if dropped. Never raises ``QueueFull``."""
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self._dropped_since_log += 1
            self._dropped_total += 1
            now = time.monotonic()
            if now - self._last_log_ts >= self._log_interval:
                log.warning(
                    "[capture] %s queue full — dropped %d frame(s) "
                    "(%d total this session); consumer fell behind",
                    self._label, self._dropped_since_log, self._dropped_total,
                )
                self._last_log_ts = now
                self._dropped_since_log = 0
            return False

    @property
    def dropped_total(self) -> int:
        return self._dropped_total
