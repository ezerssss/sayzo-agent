"""Defensive monkey-patches for pywebview's close handlers.

Pywebview 5.4's close handlers — ``BrowserView.BrowserForm.on_close`` on
Windows and ``BrowserView.WindowDelegate.windowWillClose_`` on macOS — both
unconditionally do ``del BrowserView.instances[uid]`` when the form's
``FormClosed`` / ``windowWillClose:`` event fires. If the handler runs a
second time for the same form, the second ``del`` raises ``KeyError(uid)``
(with ``uid`` typically ``'master'`` for the first window in the process).
On Windows the exception escapes through pythonnet's
``__System_Windows_Forms_FormClosedEventHandlerDispatcher`` into the .NET
runtime and surfaces as the OS-level "unhandled exception" dialog, which
also blocks the form's ``Close()`` call from completing — visible to the
user as a stuck Settings subprocess that the agent has to terminate.

Multiple close handler invocations can happen via:

  * ``Form.Close()`` being called more than once before the first
    invocation finishes the close (e.g. a race between two ``destroy()``
    callers, or a second ``Close()`` queued by ``Application.Exit()`` while
    the first is still in flight). pywebview's ``destroy_window`` reads
    ``BrowserView.instances`` outside any lock, so two callers can both
    see a non-None instance and queue independent ``Form.Close()`` calls
    on the UI thread.
  * NSWindow firing ``windowWillClose:`` more than once during teardown
    on macOS. Each call hits the same unguarded ``del``.

The patch installs an idempotency guard at the very top of each handler:
the second and subsequent invocations short-circuit instead of raising.
The first invocation does the real cleanup; subsequent ones are no-ops.

Apply with :func:`apply` once, before any ``webview.create_window`` call.
The function is idempotent and safe to call from multiple entry points.
"""
from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_applied = False


def apply() -> None:
    """Install the close-handler guards. Idempotent.

    Must run before ``webview.create_window`` so the guarded handler is
    bound to every BrowserForm / WindowDelegate that pywebview spawns.
    """
    global _applied
    with _lock:
        if _applied:
            return

        try:
            if sys.platform == "win32":
                _patch_winforms()
            elif sys.platform == "darwin":
                _patch_cocoa()
        except Exception:
            log.warning(
                "[pywebview_patches] failed to apply close-handler guards; "
                "shutdown crashes from double FormClosed / windowWillClose "
                "may surface",
                exc_info=True,
            )
            return

        _applied = True
        log.info("[pywebview_patches] close-handler guards installed")


def _patch_winforms() -> None:
    """Wrap ``BrowserView.BrowserForm.on_close`` with an idempotency guard.

    The flag is stored on ``self`` (the BrowserForm instance), so each
    window has its own latch — closing window A doesn't suppress on_close
    for window B. Belt-and-braces: the inner ``del`` in the original
    handler still gets run on the first call, but if some race or future
    pywebview change leaves ``BrowserView.instances`` short of ``self.uid``
    on entry, swallow the KeyError so it can't escape into pythonnet's
    .NET dispatcher (which is what surfaces as the OS unhandled-exception
    dialog).
    """
    from webview.platforms.winforms import BrowserView

    original = BrowserView.BrowserForm.on_close

    def guarded_on_close(self, *args):
        if getattr(self, "_sayzo_close_done", False):
            return
        self._sayzo_close_done = True
        try:
            return original(self, *args)
        except KeyError:
            log.warning(
                "[pywebview_patches] on_close KeyError suppressed for uid=%s",
                getattr(self, "uid", "<unknown>"),
                exc_info=True,
            )

    BrowserView.BrowserForm.on_close = guarded_on_close


def _patch_cocoa() -> None:
    """Wrap ``BrowserView.WindowDelegate.windowWillClose_`` with the same guard.

    The flag lives on the BrowserView instance (looked up via
    ``BrowserView.get_instance``) rather than the delegate itself,
    because a single delegate class instance is bound to many windows.
    Per-window latching matches the Windows path's per-form latching.
    """
    from webview.platforms.cocoa import BrowserView

    original = BrowserView.WindowDelegate.windowWillClose_

    def guarded_windowWillClose_(self, notification):
        i = BrowserView.get_instance("window", notification.object())
        if i is None:
            return
        if getattr(i, "_sayzo_close_done", False):
            return
        i._sayzo_close_done = True
        try:
            return original(self, notification)
        except KeyError:
            log.warning(
                "[pywebview_patches] windowWillClose_ KeyError suppressed for uid=%s",
                getattr(i, "uid", "<unknown>"),
                exc_info=True,
            )

    BrowserView.WindowDelegate.windowWillClose_ = guarded_windowWillClose_
