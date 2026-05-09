"""macOS NSApplicationDelegate hook for surfacing Settings on user re-launch.

Sayzo's macOS bundle is ``LSUIElement=True``, which means clicking the .app
in the Dock / Finder / Spotlight while the agent is already running does
**not** spawn a second process — LaunchServices delivers a
``kAEReopenApplication`` Apple Event to the existing app. Without a handler
the event is silently dropped, and the user sees no response.

Why we use NSApplicationDelegate (not NSAppleEventManager directly)
-------------------------------------------------------------------

The earlier implementation registered an ``NSAppleEventManager`` event
handler for ``kAEReopenApplication`` via
``setEventHandler:andSelector:forEventClass:andEventID:``. That looked
correct in isolation but lost a race against AppKit:

1. We registered our handler.
2. ``tray.run_main()`` called ``NSApp.run()``.
3. ``NSApp.run()`` invoked ``[NSApp finishLaunching]``, which **registers
   NSApplication's own default handler** for ``kAEReopenApplication`` —
   replacing ours. (``setEventHandler`` is replace-not-chain.)
4. NSApp's default handler forwards to
   ``[NSApp.delegate applicationShouldHandleReopen:hasVisibleWindows:]``.
   pystray never sets ``NSApp.delegate``, so the event silently no-ops.

Diagnosed from a Sequoia 15.5 user log (2026-05-10): the
``[mac_reopen] installed kAEReopenApplication handler`` line appears at
boot and is followed seconds later by ``tray icon starting``, then
nothing — Spotlight clicks while the agent runs produce zero log
output downstream. Confirmed the handler was being shadowed by
``finishLaunching``.

The canonical Apple-recommended path is to set an ``NSApplicationDelegate``
that implements ``applicationShouldHandleReopen:hasVisibleWindows:``.
NSApp's ``finishLaunching`` registers itself as the AppleEvent handler
specifically so it can dispatch to the delegate, so a delegate hook is
ALWAYS called regardless of when the delegate was set. That's the
contract we want.

Threading and lifecycle notes
-----------------------------

* ``NSApp.setDelegate_`` does NOT retain the delegate. We hold a
  module-level strong ref so PyObjC GC doesn't reap it — exact same
  reason the old NSAppleEventManager path needed it.
* The delegate's hook runs on the AppKit main thread. Any work that
  touches asyncio state must marshal back via
  ``loop.call_soon_threadsafe``. Callers wire that themselves; we just
  invoke the callback directly. (``threading.Event.set`` happens to
  be thread-safe, which is what the agent passes in.)
* Pystray's ``IconDelegate`` is the target of the NSStatusItem button
  action — it is NOT installed as ``NSApp.delegate``. Setting our
  delegate does not conflict.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Module-level strong ref to the delegate so PyObjC GC doesn't collect it.
# NSApp.setDelegate_ is non-retaining; without this, the delegate would be
# GC'd before the user ever clicks Sayzo, and reopen events would resume
# being silently dropped.
_delegate_singleton: Any = None


def install_reopen_handler(callback: Callable[[], None]) -> Optional[Any]:
    """Register an ``NSApplicationDelegate`` hook for app-reopen events.

    The hook invokes ``callback()`` (no arguments) whenever LaunchServices
    delivers a reopen event — fires on Dock click, Spotlight launch,
    ``open -a Sayzo``, Finder Applications double-click while the app is
    already running.

    Returns the delegate object on success (caller may keep it for
    parity; a module-level strong ref is held internally so the caller
    does NOT have to). Returns ``None`` on non-darwin or any
    registration failure — failures are logged and never propagate, so
    the agent boot path is unaffected if PyObjC is missing or AppKit
    raises.

    Idempotent: re-installing replaces the previous delegate. The old
    delegate object is dropped and may be GC'd; AppKit will pick up the
    new one on the next reopen event.
    """
    global _delegate_singleton

    if sys.platform != "darwin":
        return None

    try:
        from AppKit import NSApplication  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except Exception:
        log.warning(
            "[mac_reopen] PyObjC unavailable — Dock-click won't open Settings",
            exc_info=True,
        )
        return None

    class _ReopenDelegate(NSObject):
        def initWithCallback_(self, cb):  # type: ignore[no-untyped-def]
            self = objc.super(_ReopenDelegate, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        # PyObjC selector signature: trailing colons in the Obj-C
        # selector ``applicationShouldHandleReopen:hasVisibleWindows:``
        # map to underscores in the Python method name.
        def applicationShouldHandleReopen_hasVisibleWindows_(  # type: ignore[no-untyped-def]
            self, app, has_visible_windows
        ):
            try:
                self._cb()
            except Exception:
                log.warning("[mac_reopen] callback raised", exc_info=True)
            # Returning False prevents NSApp's default reopen behavior
            # (unminimize windows / bring to front). We have no NSWindows
            # on the agent process — only the menubar item — so True vs
            # False is roughly equivalent here, but False is the honest
            # answer ("we handled it ourselves") and matches what an
            # LSUIElement app should report.
            return False

    try:
        delegate = _ReopenDelegate.alloc().initWithCallback_(callback)
        # Touching ``sharedApplication`` here is intentional: it
        # initializes NSApp if no caller has done so yet, so
        # ``setDelegate_`` always has a valid ``NSApp`` instance to bind
        # to. pystray will hit the same ``sharedApplication`` later and
        # get back the singleton — no double-init.
        app = NSApplication.sharedApplication()
        app.setDelegate_(delegate)
    except Exception:
        log.warning("[mac_reopen] failed to install delegate", exc_info=True)
        return None

    _delegate_singleton = delegate
    log.info(
        "[mac_reopen] installed NSApplicationDelegate "
        "applicationShouldHandleReopen:hasVisibleWindows:"
    )
    return delegate
