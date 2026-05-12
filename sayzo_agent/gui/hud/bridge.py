"""JS-callable bridge exposed to the HUD's React app.

A single method, :meth:`HudBridge.hud_event`, is exposed to JavaScript
via pywebview's ``js_api`` mechanism. The React side calls it whenever it
needs to ship a response or telemetry event back to the parent agent
(consent card answer, actionable button outcome, pill button click,
log). The bridge serializes the payload as a newline-delimited JSON
record on ``sys.stdout`` — the parent's stdout reader thread picks it
up and dispatches it.

Stdout is deliberately the only outbound channel — it's the same pipe
the parent already holds open on the subprocess handle, so no separate
IPC server / port file / TCP socket is needed. The Settings subprocess
uses a loopback-TCP IPC server for similar talkback, but Settings is
spawned ad-hoc by the user and runs as a sibling of the agent; the HUD
is always a direct child, so the pipe is open by construction.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)


class HudBridge:
    """js_api object for the HUD pywebview window.

    Thread-safe: writes to stdout are serialized through a single lock
    so two simultaneous JS calls can't interleave bytes.
    """

    def __init__(self) -> None:
        self._stdout_lock = threading.Lock()
        # Out-of-band readiness latch: HudWindow waits on this before
        # flushing any commands queued during early startup. JS calls
        # ``hud_event({"event": "hud_ready"})`` once the React app has
        # mounted and the subscriber is attached.
        self.ready_event = threading.Event()

    def hud_event(self, payload: Any) -> None:
        """Called from JavaScript with an arbitrary JSON-serialisable payload.

        We treat the JS side as authoritative — every well-formed event
        is forwarded verbatim. Malformed payloads are logged and dropped
        rather than crashing the bridge.
        """
        try:
            text = json.dumps(payload)
        except (TypeError, ValueError):
            log.warning(
                "[hud-bridge] dropped non-serialisable payload: %r", payload
            )
            return

        # Tap the ready latch early so the launcher's wait_for_ready
        # call unblocks even if the parent isn't actively reading stdout.
        event = payload.get("event") if isinstance(payload, dict) else None
        if event == "hud_ready":
            self.ready_event.set()

        with self._stdout_lock:
            try:
                sys.stdout.write(text)
                sys.stdout.write("\n")
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                # Parent died or closed our stdout. There's nothing we
                # can do — log and continue (the next quit-on-EOF in the
                # stdin reader will tear us down).
                log.warning(
                    "[hud-bridge] stdout broken — parent may have died"
                )
            except Exception:
                log.warning(
                    "[hud-bridge] failed to write event to stdout",
                    exc_info=True,
                )
