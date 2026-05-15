"""macOS-side shutdown observers — counterpart to ``win_shutdown.py``.

Why this exists
---------------

The Sayzo agent process (the Python service that owns the HUD subprocess
via ``HudLauncher``) needs to detect OS-initiated shutdown so it can
push a ``quit_sync`` command to the HUD before the OS kills everything.
This is the agent-side defense-in-depth for the v2.16.0 HUD shutdown
hardening — the HUD subprocess's own Qt ``commitDataRequest`` handler is
the primary fix, but the agent forwarding a quit signal independently
removes a single point of failure (see ``zesty-zooming-taco.md`` v2.16.0
plan, RC-5).

macOS uses ``NSWorkspace.sharedWorkspace.notificationCenter`` to post
``NSWorkspaceWillPowerOffNotification`` when the user initiates a
shutdown / restart. We register a Cocoa observer on it.

Apple's docs note that this notification is delivered to all observers
on the main thread before the user-visible shutdown UI proceeds; we
have a small but real window to act.

No-op on non-darwin, returns False so callers can branch cleanly.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# Module-level handle on the live observer so it isn't GC'd. PyObjC's
# observer registration takes a Python object as the target; if the
# Python object is collected the underlying ObjC reference points at
# freed memory and the notification dispatch crashes.
_observer_handle: Optional[Any] = None


def observe_will_power_off(callback: Callable[[], None]) -> bool:
    """Register ``callback`` against ``NSWorkspaceWillPowerOffNotification``.

    Returns ``True`` if the observer was registered. Returns ``False``
    on non-darwin (silently) or if pyobjc imports fail (logged).

    The callback runs on the main Cocoa thread when macOS posts the
    notification. It should return quickly — heavy work blocks the
    shutdown handshake.

    Only the first call installs the observer; subsequent calls return
    ``True`` without re-registering, so callers can be idempotent.
    Calling multiple times with different callbacks is unsupported (we
    only keep the first one to avoid bookkeeping for a use case we
    don't have today).
    """
    global _observer_handle
    if sys.platform != "darwin":
        return False
    if _observer_handle is not None:
        return True

    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except Exception:
        log.warning(
            "[mac_shutdown] AppKit / Foundation import failed — "
            "shutdown observer NOT installed",
            exc_info=True,
        )
        return False

    class _ShutdownObserver(NSObject):  # type: ignore[misc]
        # Stored as an instance attribute so PyObjC's metaclass machinery
        # finds it at registration time. Set during ``init``.
        _py_callback = None

        def initWithCallback_(self, cb):  # noqa: N802 — ObjC selector
            # ``NSObject.init(self)`` raises "Need 0 arguments, got 1" under
            # PyObjC because ``init`` is a 0-arg selector — the receiver
            # is implicit. Use ``objc.super`` to dispatch to the parent
            # init the way every other PyObjC subclass in this codebase
            # does (see ``mac_reopen.py::_ReopenDelegate``).
            self = objc.super(_ShutdownObserver, self).init()
            if self is None:
                return None
            self._py_callback = cb
            return self

        def willPowerOff_(self, notification):  # noqa: N802 — ObjC selector
            log.warning(
                "[mac_shutdown] NSWorkspaceWillPowerOffNotification fired"
            )
            cb = self._py_callback
            if cb is None:
                return
            try:
                cb()
            except Exception:
                log.warning(
                    "[mac_shutdown] WillPowerOff callback raised", exc_info=True
                )

    try:
        ws = NSWorkspace.sharedWorkspace()
        center = ws.notificationCenter()
        observer = _ShutdownObserver.alloc().initWithCallback_(callback)
        center.addObserver_selector_name_object_(
            observer,
            b"willPowerOff:",
            "NSWorkspaceWillPowerOffNotification",
            None,
        )
        _observer_handle = observer
        log.info("[mac_shutdown] NSWorkspaceWillPowerOff observer installed")
        return True
    except Exception:
        log.warning(
            "[mac_shutdown] NSWorkspace observer registration failed",
            exc_info=True,
        )
        return False
