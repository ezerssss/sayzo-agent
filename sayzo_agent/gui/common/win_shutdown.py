"""Windows-shutdown protection for pywebview windows.

Why this exists (the gap left by ``safe_quit.py``)
--------------------------------------------------

``safe_quit_window`` protects our *explicit* quit path: when the parent
agent sends ``quit`` over stdin, ``_dispatch_quit`` calls it and the
message loop exits via ``WM_QUIT`` without firing ``FormClosed``.

But the parent agent isn't the only thing that closes the Settings
subprocess. Windows shutdown sends ``WM_CLOSE`` directly to every
top-level window. That bypasses ``_dispatch_quit`` entirely:
``Form.WmClose → Form.OnFormClosed → pywebview's on_close (Python) →
Control.Invoke(_shutdown) → MarshaledInvoke`` — and by the time
``MarshaledInvoke`` runs, Windows has typically already killed the
WebView2 browser child process (msedgewebview2.exe). Control.Invoke
checks the process owning the control's HWND, sees it's dead, and
throws ``System.ArgumentException("Process with an Id of N is not
running")``. That escapes through pythonnet's FormClosedEventHandler
dispatcher into .NET's unhandled-exception JIT-debugging dialog, which
*blocks Windows shutdown* until the user dismisses it.

Reported externally as: ".NET framework dialog is preventing my PC
from shutting down."

What this module does
---------------------

Two layers of defence, both Windows-only:

1. **SessionEnding handler** — subscribe to
   ``Microsoft.Win32.SystemEvents.SessionEnding``, which fires on
   ``WM_QUERYENDSESSION`` (the message Windows sends *before*
   ``WM_CLOSE`` to ask permission to shut down). In the handler we set
   the ``_quitting`` flag (so the idle Settings hide-on-close
   contract lets the close proceed) and call ``safe_quit_window`` —
   which posts ``WM_QUIT`` via ``BeginInvoke``. The message loop
   processes ``WM_QUIT`` first, so ``WM_CLOSE``'s ``OnFormClosed``
   path never runs and the crash never happens.

2. **Application.ThreadException safety net** — if anything still
   slips through (X-button race, OS variant we haven't observed,
   pywebview internal changes), we register a ``ThreadException``
   handler that logs the exception and silently swallows it. The
   JIT-debugging dialog never appears; shutdown isn't blocked. We
   only swallow inside ``BrowserForm`` / pywebview-internal stack
   frames so genuine application bugs still surface normally.

We could have monkey-patched pywebview's ``on_close`` to fix the root
cause (and tried in v2.7.5 — see
``project_pywebview_close_guard_reverted.md``). The patch regressed
the idle Settings tab-switch path and was reverted. The "intercept
before WM_CLOSE arrives" approach in this module has a much smaller
blast radius: it only fires on actual session-end, never touches
pywebview's classes, and the safety net runs at unhandled-exception
time on a thread that's already on its way out.

This module is no-op on macOS / Linux. macOS has no equivalent JIT
dialog and pywebview's Cocoa close path doesn't recurse the way
``Application.Exit()`` does on WinForms.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)


def install_shutdown_protection(
    window: "webview.Window",
    *,
    set_quitting: Optional[Callable[[], None]] = None,
) -> None:
    """Install Windows-shutdown protection on a pywebview window.

    ``set_quitting`` is called from the SessionEnding handler before
    ``safe_quit_window`` so callers that distinguish quit-vs-hide on
    the close path (the idle Settings window's ``on_closing``) can
    flip into quit mode. Pass ``None`` if there's no such flag (the
    Setup window, which has no idle-mode contract).

    Best-effort: any failure logs and swallows — these handlers are
    pure belt-and-suspenders. The agent works without them; we just
    crash on shutdown the way we did before.
    """
    if sys.platform != "win32":
        return

    _install_session_ending_handler(window, set_quitting=set_quitting)
    _install_thread_exception_handler()


def _install_session_ending_handler(
    window: "webview.Window",
    *,
    set_quitting: Optional[Callable[[], None]],
) -> None:
    """Subscribe to SystemEvents.SessionEnding → safe_quit_window.

    SystemEvents fires this on ``WM_QUERYENDSESSION``, before Windows
    starts terminating processes. We have a small window to post
    ``WM_QUIT`` and let the message loop drain cleanly.

    Imports are lazy so this module stays cheap to import and doesn't
    trigger pythonnet / .NET assembly load at module-load time. By
    install-time, pywebview is fully running and these are cached.
    """
    try:
        from Microsoft.Win32 import SystemEvents, SessionEndingEventHandler
    except Exception:
        log.warning(
            "[win_shutdown] SystemEvents import failed — shutdown will not "
            "be intercepted cleanly",
            exc_info=True,
        )
        return

    from sayzo_agent.gui.common.safe_quit import safe_quit_window

    def _on_session_ending(sender, args) -> None:
        # ``args.Reason`` is SessionEndReasons.Logoff or .SystemShutdown.
        # We treat both identically: exit cleanly before pywebview's
        # FormClosed handler crashes.
        reason = "unknown"
        try:
            reason = str(args.Reason)
        except Exception:
            pass
        log.warning(
            "[win_shutdown] SessionEnding fired (reason=%s) — quitting via "
            "safe_quit_window before WM_CLOSE arrives",
            reason,
        )
        if set_quitting is not None:
            try:
                set_quitting()
            except Exception:
                log.warning(
                    "[win_shutdown] set_quitting callback raised", exc_info=True
                )
        # Belt-and-suspenders: if the message loop hasn't drained within
        # the timeout, force-exit so we never block Windows shutdown waiting
        # on a pywebview internal hang. Windows kills GUI apps after ~5s of
        # not responding to WM_ENDSESSION; ``_HARD_EXIT_TIMEOUT_SECS`` is
        # set well inside that budget.
        _arm_hard_exit_timer()
        try:
            safe_quit_window(window)
        except Exception:
            log.warning("[win_shutdown] safe_quit_window raised", exc_info=True)

    try:
        SystemEvents.SessionEnding += SessionEndingEventHandler(_on_session_ending)
        log.info("[win_shutdown] SessionEnding handler installed")
    except Exception:
        log.warning(
            "[win_shutdown] SessionEnding subscription failed", exc_info=True
        )


def _install_thread_exception_handler() -> None:
    """Install a WinForms ThreadException handler that swallows pywebview
    teardown crashes.

    Only swallows when the stack contains pywebview internals — genuine
    application errors still propagate to the default handler so we don't
    mask real bugs.

    Idempotent: subscribing the same handler twice has no extra effect
    beyond the second add, and on shutdown the duplicate fires twice
    (also harmless — both calls log + swallow).
    """
    try:
        import System.Windows.Forms as WinForms
        from System.Threading import ThreadExceptionEventHandler
    except Exception:
        log.warning(
            "[win_shutdown] WinForms import failed — JIT-dialog safety net "
            "is not installed",
            exc_info=True,
        )
        return

    def _on_thread_exception(sender, args) -> None:
        # ``args.Exception`` is the .NET Exception. We don't have a clean
        # Python traceback from here, but the .NET ToString gives the full
        # CLR stack including the pywebview / FormClosed frames.
        try:
            exc_text = str(args.Exception)
        except Exception:
            exc_text = "<unprintable>"

        if _looks_like_pywebview_teardown_crash(exc_text):
            log.warning(
                "[win_shutdown] swallowed pywebview teardown exception "
                "(prevents JIT dialog blocking shutdown):\n%s",
                exc_text,
            )
            return

        # Anything else — log and let the default handler take over so we
        # don't mask real bugs. The default handler shows the JIT dialog,
        # which is what we want for a genuine app error.
        log.error(
            "[win_shutdown] unhandled WinForms thread exception (not "
            "swallowed — looks like a real bug, not a shutdown teardown "
            "crash):\n%s",
            exc_text,
        )

    try:
        WinForms.Application.ThreadException += ThreadExceptionEventHandler(
            _on_thread_exception
        )
        # Critical: switching the unhandled-exception mode lets our handler
        # run instead of .NET's "send to JIT debugger" dialog. Without this,
        # ThreadException still fires but the default JIT dialog also shows.
        try:
            WinForms.Application.SetUnhandledExceptionMode(
                WinForms.UnhandledExceptionMode.CatchException,
                # threadScope=False: apply the mode to ALL .NET threads in
                # the subprocess, not just the calling thread. pywebview
                # spawns a separate STA thread for ``Application.Run()``
                # (see ``webview/platforms/winforms.py:683-686``), so the
                # mode set here on Python's main thread would otherwise
                # never reach the thread that actually fires FormClosed —
                # which is why every prior "the safety net should have
                # caught this" report slipped through. This is a Settings/
                # Setup subprocess; it has no other "important" threads to
                # protect from over-catching, and our filter at
                # ``_looks_like_pywebview_teardown_crash`` still lets real
                # bugs surface through the default handler.
                False,
            )
        except Exception:
            log.warning(
                "[win_shutdown] SetUnhandledExceptionMode failed", exc_info=True
            )
        log.info("[win_shutdown] ThreadException safety net installed")
    except Exception:
        log.warning(
            "[win_shutdown] ThreadException subscription failed", exc_info=True
        )


# Substrings that identify a pywebview / WinForms teardown crash we want to
# silently swallow at shutdown. The full .NET stack contains qualified type
# names; matching on any one of these is sufficient because the alternative
# (a genuine app-thread bug landing here at shutdown) is vanishingly rare and
# the worst case is a logged warning instead of a JIT dialog.
_PYWEBVIEW_TEARDOWN_SIGNATURES = (
    "FormClosedEventHandlerDispatcher",
    "Python.Runtime.Dispatcher",
    "Form.OnFormClosed",
    "Form.WmClose",
    "BrowserForm",
    # Known exception signatures from pywebview's on_close path during shutdown:
    #   1. v2.7.5 / KeyError('master') — `del BrowserView.instances[uid]`
    #   2. v2.7.11 trigger — Control.Invoke against a dead WebView2 process
    #   3. v2.14.1 trigger — EdgeChrome.clear_user_data reading BrowserProcessId
    #      while CoreWebView2 is still None (form closed pre-init). Root-caused
    #      in v2.15.0 by setting ``private_mode=False`` on webview.start(),
    #      which skips clear_user_data entirely.
    #   4. v2.15.0 — InvalidComObjectException / MarshaledInvoke against a
    #      detached RCW at Windows shutdown (WebView2 child process killed
    #      first by the OS). Root-caused at the BrowserForm.on_close layer
    #      by patch_on_close_swallow_teardown in pywebview_patches.py; this
    #      signature is the layer-2 safety net.
    "Process with an Id of",
    "KeyError",
    "BrowserProcessId",
    "clear_user_data",
    "InvalidComObjectException",
    "MarshaledInvoke",
    "separated from its underlying RCW",
)


def _looks_like_pywebview_teardown_crash(exc_text: str) -> bool:
    """Heuristic: does this .NET exception look like the pywebview shutdown bug?"""
    return any(sig in exc_text for sig in _PYWEBVIEW_TEARDOWN_SIGNATURES)


# Hard timeout for the Settings/Setup subprocess after SessionEnding fires.
# Windows gives GUI apps ~5 s to respond to WM_ENDSESSION before killing
# them with WM_CLOSE / TerminateProcess. We pick a value well inside that
# budget so the message loop has time to drain WM_QUIT first; if it
# doesn't (pywebview internal hang, deadlocked Dispose, etc.), we exit
# the process unconditionally. Better to die slightly early than block
# the user's shutdown waiting on a stuck WebView2 teardown.
_HARD_EXIT_TIMEOUT_SECS = 2.0


def _arm_hard_exit_timer() -> None:
    """Schedule ``os._exit(0)`` after ``_HARD_EXIT_TIMEOUT_SECS``.

    Daemon thread so it doesn't block clean shutdown. If the message
    loop drains and the process exits cleanly first, this thread dies
    with the process. If something hangs, the timer fires and the
    process exits without any further cleanup — by design, since at
    SessionEnding time the OS is about to reclaim our resources anyway.
    """
    def _fire():
        log.warning(
            "[win_shutdown] hard-exit timer fired after %.1fs — forcing "
            "process exit so Windows shutdown isn't blocked",
            _HARD_EXIT_TIMEOUT_SECS,
        )
        os._exit(0)

    timer = threading.Timer(_HARD_EXIT_TIMEOUT_SECS, _fire)
    timer.daemon = True
    timer.start()
