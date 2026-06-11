"""JS-callable bridge exposed to the HUD's React app via QWebChannel.

Four slots are exposed to JavaScript:

* :meth:`set_window_visible(visible)` — toggles the host ``QWidget``
  between its top-right (or last-dragged) anchor and the offscreen
  anchor.
* :meth:`set_window_size(width, height)` — resizes the host widget
  to the React-reported content rectangle.
* :meth:`start_system_move()` — hands a native window drag off to
  Qt at the cursor's current position.
* :meth:`hud_event(payload_json)` — forwards a React-emitted event
  (``card_response``, ``actionable_response``, ``hud_ready``, …) to
  ``sys.stdout`` for the parent agent's launcher to pick up.

Visibility and size hooks are buffered: if React calls them before
the HUD window has registered its callback (cold-boot race between
QWebChannel setup and ``QWebEngineView.loadFinished``), the latest
request replays on registration.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Slot

log = logging.getLogger(__name__)


class HudBridge(QObject):
    """QObject exposed to the HUD's React app via QWebChannel.

    Thread-safe: writes to stdout are serialized through a lock so two
    simultaneous JS calls can't interleave bytes.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stdout_lock = threading.Lock()
        # Readiness latch — set when JS calls ``hud_event({"event":
        # "hud_ready"})`` after the React app has mounted.
        self.ready_event = threading.Event()
        # In-process callbacks registered by :class:`HudWindow` once the
        # underlying QWidget handle is realised. The JS slots forward
        # through these instead of doing window manipulation directly,
        # so unit tests can swap in fakes.
        self._set_visible_cb: Optional[Callable[[bool], None]] = None
        self._set_size_cb: Optional[Callable[[int, int], None]] = None
        # Native-drag hook. The JS side calls
        # :meth:`start_system_move` from a mousedown handler on
        # ``.hud-drag`` regions; this callback (registered by
        # :class:`HudWindow`) asks Qt to begin a native window drag
        # via ``QWindow.startSystemMove()``. No buffering — drag is
        # user-initiated so it can never fire before the callback is
        # registered.
        self._start_system_move_cb: Optional[Callable[[], None]] = None
        # If React calls a slot before the callback is wired, we buffer
        # the latest request and replay it on registration. Earlier
        # buffered values are dropped — the latest is authoritative.
        self._pending_visibility: Optional[bool] = None
        self._pending_size: Optional[tuple[int, int]] = None
        self._window_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Python-only callback registration (no @Slot — not exposed to JS).
    # ------------------------------------------------------------------

    def set_visibility_callback(
        self, cb: Optional[Callable[[bool], None]],
    ) -> None:
        """Register the function the JS side will invoke to hide/show the window."""
        with self._window_lock:
            self._set_visible_cb = cb
            pending = self._pending_visibility
            self._pending_visibility = None
        if cb is not None and pending is not None:
            try:
                cb(pending)
            except Exception:
                log.warning(
                    "[hud-bridge] replayed visibility call raised", exc_info=True
                )

    def set_size_callback(
        self, cb: Optional[Callable[[int, int], None]],
    ) -> None:
        """Register the function the JS side will invoke to resize the window."""
        with self._window_lock:
            self._set_size_cb = cb
            pending = self._pending_size
            self._pending_size = None
        if cb is not None and pending is not None:
            try:
                cb(pending[0], pending[1])
            except Exception:
                log.warning(
                    "[hud-bridge] replayed size call raised", exc_info=True
                )

    def set_start_system_move_callback(
        self, cb: Optional[Callable[[], None]],
    ) -> None:
        """Register the callback the JS-callable :meth:`start_system_move` fires.

        :class:`HudWindow` registers a closure that calls
        ``self.windowHandle().startSystemMove()`` so the OS takes over
        the drag at the cursor's current position.
        """
        self._start_system_move_cb = cb

    # ------------------------------------------------------------------
    # JS-callable slots.
    # ------------------------------------------------------------------

    @Slot(bool)
    def set_window_visible(self, visible: bool) -> None:
        """Hide or show the HUD host window."""
        target = bool(visible)
        with self._window_lock:
            cb = self._set_visible_cb
            if cb is None:
                self._pending_visibility = target
                return
        try:
            cb(target)
        except Exception:
            log.warning(
                "[hud-bridge] set_window_visible callback raised", exc_info=True
            )

    @Slot(int, int)
    def set_window_size(self, width: int, height: int) -> None:
        """Resize the HUD host window to the React-reported content rect."""
        try:
            w = max(1, int(width))
            h = max(1, int(height))
        except (TypeError, ValueError):
            log.warning(
                "[hud-bridge] set_window_size got non-int args: %r, %r",
                width, height,
            )
            return
        with self._window_lock:
            cb = self._set_size_cb
            if cb is None:
                self._pending_size = (w, h)
                return
        try:
            cb(w, h)
        except Exception:
            log.warning(
                "[hud-bridge] set_window_size callback raised", exc_info=True
            )

    @Slot()
    def start_system_move(self) -> None:
        """Begin a native Qt window drag.

        Called from the React app's mousedown handler when the user
        clicks-and-drags on a ``.hud-drag`` region. Forwards to the
        host widget which calls ``QWindow.startSystemMove()`` — the OS
        then handles cursor tracking, snapping, and release natively.
        """
        cb = self._start_system_move_cb
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.warning(
                "[hud-bridge] start_system_move callback raised", exc_info=True
            )

    @Slot(str)
    def hud_event(self, payload_json: str) -> None:
        """Forward a React-emitted event to the parent agent via stdout.

        Receives a JSON-stringified payload (the JS side does
        ``JSON.stringify(event)`` to keep marshaling simple — QWebChannel
        does support QJsonObject auto-marshaling but stringified JSON is
        more predictable for our use case).
        """
        if not isinstance(payload_json, str):
            log.warning(
                "[hud-bridge] dropped non-string payload: %r", type(payload_json),
            )
            return

        # Substring-peek for the ready latch instead of parse+re-dump
        # round-tripping every event — the parent reader does its own
        # parse anyway and audio-level updates run at ~50 ms cadence.
        if (
            '"event":"hud_ready"' in payload_json
            or '"event": "hud_ready"' in payload_json
        ):
            self.ready_event.set()

        self._write_line(payload_json)

    def emit_event(self, payload: dict) -> None:
        """Write a Python-originated event to stdout (e.g. the heartbeat
        ``pong``). Same serialized stdout path as :meth:`hud_event` so
        the parent reader sees one well-formed line; the lock prevents
        interleaving with a concurrent JS-originated event.
        """
        import json

        try:
            line = json.dumps(payload)
        except (TypeError, ValueError):
            log.warning("[hud-bridge] emit_event: non-serialisable payload")
            return
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        with self._stdout_lock:
            try:
                sys.stdout.write(line)
                sys.stdout.write("\n")
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                log.warning(
                    "[hud-bridge] stdout broken — parent may have died"
                )
            except Exception:
                log.warning(
                    "[hud-bridge] failed to write event to stdout",
                    exc_info=True,
                )
