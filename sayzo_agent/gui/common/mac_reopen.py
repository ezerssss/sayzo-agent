"""macOS NSAppleEventManager hook for surfacing Settings on user re-launch.

Sayzo's macOS bundle is ``LSUIElement=True``, which means clicking the .app
in the Dock / Finder / Spotlight while the agent is already running does
**not** spawn a second process — LaunchServices delivers a
``kAEReopenApplication`` Apple Event to the existing app. Without a handler
the event is silently dropped, and the user sees no response.

This module installs an ``NSAppleEventManager`` handler that runs the
caller-supplied ``callback`` whenever a reopen event arrives. The agent
wires that callback to ``loop.call_soon_threadsafe(state.settings_event.set)``
so the existing tray-Settings code path takes over from there.

The handler operates at the AppleEvent layer, NOT the NSApplicationDelegate
layer — pystray's NSStatusItem usage is unaffected.

PyObjC notes:

* ``setEventHandler_andSelector_forEventClass_andEventID_`` does NOT retain
  the handler, so we keep a module-level strong reference. If the only ref
  is local to ``install_reopen_handler``, GC eventually collects it and the
  events stop firing — a footgun documented in the PyObjC mailing list.
* The handler runs on the main (NSApp) thread. Any work that touches
  asyncio state must marshal back via ``loop.call_soon_threadsafe`` —
  callers handle that, this module just invokes the callback directly.
* The Apple Event four-char codes (``'aevt'`` / ``'rapp'``) are converted
  to their UInt32 representation here rather than imported from
  ``Carbon``, since the Carbon module is deprecated and not consistently
  available across PyObjC versions.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Module-level strong ref to the handler so PyObjC GC doesn't collect it.
# AppleEvents stop firing if the handler object goes away.
_handler_singleton: Any = None


def _fourcc(code: str) -> int:
    """Pack a 4-byte ASCII string (e.g. ``'aevt'``) into a UInt32 FourCC."""
    if len(code) != 4:
        raise ValueError(f"FourCC must be 4 chars, got {code!r}")
    return (
        (ord(code[0]) << 24)
        | (ord(code[1]) << 16)
        | (ord(code[2]) << 8)
        | ord(code[3])
    )


def install_reopen_handler(callback: Callable[[], None]) -> Optional[Any]:
    """Register an NSAppleEventManager handler for ``kAEReopenApplication``.

    The handler invokes ``callback()`` (with no arguments) whenever
    LaunchServices delivers a reopen event — that fires on Dock click,
    Spotlight launch, ``open -a Sayzo``, and Finder Applications double-click
    while the app is already running.

    Returns the handler object on success (caller may keep it for parity;
    a module-level strong ref is held internally so the caller does NOT
    have to). Returns ``None`` on non-darwin or any registration failure
    — failures are logged and never propagate, so the agent boot path is
    unaffected if PyObjC is missing or AppKit raises.

    Idempotent: re-installing replaces the handler. The previous handler
    object is dropped and may be GC'd.
    """
    global _handler_singleton

    if sys.platform != "darwin":
        return None

    try:
        from Foundation import NSAppleEventManager, NSObject  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except Exception:
        log.warning(
            "[mac_reopen] PyObjC unavailable — Dock-click won't open Settings",
            exc_info=True,
        )
        return None

    class _ReopenHandler(NSObject):
        def initWithCallback_(self, cb):  # type: ignore[no-untyped-def]
            self = objc.super(_ReopenHandler, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        # Selector signature matches "handleReopen:withReplyEvent:". PyObjC
        # maps trailing colons to Python underscores, so the Python method
        # name must be ``handleReopen_withReplyEvent_``.
        def handleReopen_withReplyEvent_(self, event, reply):  # type: ignore[no-untyped-def]
            try:
                self._cb()
            except Exception:
                log.warning("[mac_reopen] callback raised", exc_info=True)

    try:
        handler = _ReopenHandler.alloc().initWithCallback_(callback)
        manager = NSAppleEventManager.sharedAppleEventManager()
        manager.setEventHandler_andSelector_forEventClass_andEventID_(
            handler,
            b"handleReopen:withReplyEvent:",
            _fourcc("aevt"),  # kCoreEventClass
            _fourcc("rapp"),  # kAEReopenApplication
        )
    except Exception:
        log.warning("[mac_reopen] failed to install reopen handler", exc_info=True)
        return None

    _handler_singleton = handler
    log.info("[mac_reopen] installed kAEReopenApplication handler")
    return handler
