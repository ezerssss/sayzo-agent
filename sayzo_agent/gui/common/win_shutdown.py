"""Windows-shutdown protection for pywebview windows.

Why this exists (the gap left by the explicit quit path)
--------------------------------------------------------

The *explicit* quit path (parent agent sends ``quit`` over stdin →
``SettingsWindow._dispatch_quit``) handles itself: on Windows it
hard-exits via ``os._exit(0)`` before any .NET teardown runs, so the
pywebview FormClosed crash never happens.

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
   ``WM_CLOSE`` to ask permission to shut down). In the handler we
   **hard-exit** (``os._exit(0)``) before ``WM_CLOSE`` can drive the
   ``FormClosed`` teardown at all. Earlier versions instead posted
   ``WM_QUIT`` via ``safe_quit_window`` and armed a 2 s fallback timer,
   but the teardown that ``safe_quit_window`` triggers throws a .NET
   exception that pythonnet crashes while *marshalling* back into Python
   (``System.NullReferenceException`` in
   ``TypeManager.AllocateTypeObject`` — a known pythonnet 3.x shutdown
   race). That crash sits *below* the Python ``try/except`` and the
   WinForms ``ThreadException`` net, so neither layer can catch it; it
   surfaces as the WerFault ".NET Framework / stopped working" dialog.
   The only reliable fix is to not run the teardown — see
   ``SettingsWindow._dispatch_quit`` for the matching explicit-quit path.

2. **Application.ThreadException safety net** — if any *other* WinForms
   message-loop exception still slips through (X-button race, OS variant
   we haven't observed, pywebview internal changes), we register a
   ``ThreadException`` handler that logs the exception and silently
   swallows it. We only swallow inside ``BrowserForm`` / pywebview-
   internal stack frames so genuine application bugs still surface
   normally. Note this layer cannot catch the pythonnet-marshaller crash
   above (that's why layer 1 hard-exits) — it covers the cases where a
   .NET exception *does* reach the message loop cleanly.

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

    ``set_quitting`` is flipped by the SessionEnding handler before it
    hard-exits. Because the hard-exit (``os._exit``) pre-empts ``WM_CLOSE``,
    the idle Settings ``on_closing`` handler never runs on this path, so the
    flag is belt-and-suspenders here — it's only load-bearing on the
    explicit-quit / macOS close path (where the window is actually destroyed
    and ``on_closing`` fires). Pass ``None`` if there's no such flag (the
    Setup window, which has no idle-mode contract).

    Best-effort: any failure logs and swallows — these handlers are
    pure belt-and-suspenders. The agent works without them; we just
    crash on shutdown the way we did before.
    """
    if sys.platform != "win32":
        return

    _install_session_ending_handler(window, set_quitting=set_quitting)
    _install_thread_exception_handler()


def install_session_ending_callback(callback: Callable[[], None]) -> bool:
    """Subscribe ``callback`` to ``SystemEvents.SessionEnding``.

    Bare, window-agnostic wrapper. Used by the agent process (no
    pywebview window) to be notified at ``WM_QUERYENDSESSION`` so it
    can push a quit to the HUD subprocess via
    ``HudLauncher.quit_sync()``. The pywebview-window-aware variant
    above (``_install_session_ending_handler``) layers extra logic on
    top of this same subscription: it flips ``set_quitting`` and then
    hard-exits the subprocess (``os._exit``) before pywebview's
    FormClosed teardown can crash pythonnet.

    Returns ``True`` if subscribed; ``False`` on non-Windows or if the
    pythonnet bridge to ``Microsoft.Win32.SystemEvents`` can't be
    imported. Lazy imports so this module stays cheap to load.

    The callback runs on whichever thread ``SystemEvents`` dispatches
    on — historically the WinForms message thread for the process that
    set up the SystemEvents subscription. Callers should treat the
    callback as a high-priority "OS is shutting down NOW" signal and
    return quickly. Anything heavier than scheduling work on another
    thread risks blocking the shutdown handshake.
    """
    if sys.platform != "win32":
        return False
    # ``Microsoft.Win32.SystemEvents`` lives in the ``System.dll``
    # assembly. pythonnet only exposes a .NET namespace as a Python
    # module once that assembly has been added to the CLR's reference
    # list via ``clr.AddReference``. The pywebview Settings/Setup
    # subprocesses get this for free because pywebview itself calls
    # ``clr.AddReference('System.Windows.Forms')`` etc. at import,
    # which pulls in ``System``. But the agent process is pure Python
    # — pycaw uses comtypes, not pythonnet — so without an explicit
    # ``AddReference`` here, ``from Microsoft.Win32 import …`` raises
    # ``ModuleNotFoundError: No module named 'Microsoft'``. v2.16.0
    # shipped this function without the AddReference and the user hit
    # exactly that error in the agent's startup log.
    try:
        import clr  # type: ignore[import-not-found]

        clr.AddReference("System")
    except Exception:
        log.warning(
            "[win_shutdown] clr.AddReference('System') failed — "
            "pythonnet not available in this process",
            exc_info=True,
        )
        return False
    try:
        from Microsoft.Win32 import SystemEvents, SessionEndingEventHandler
    except Exception:
        log.warning(
            "[win_shutdown] SystemEvents import failed for "
            "install_session_ending_callback",
            exc_info=True,
        )
        return False

    def _on_session_ending(sender, args) -> None:
        reason = "unknown"
        try:
            reason = str(args.Reason)
        except Exception:
            pass
        log.warning(
            "[win_shutdown] SessionEnding callback firing (reason=%s)", reason,
        )
        try:
            callback()
        except Exception:
            log.warning(
                "[win_shutdown] SessionEnding callback raised", exc_info=True
            )

    try:
        SystemEvents.SessionEnding += SessionEndingEventHandler(_on_session_ending)
        log.info("[win_shutdown] generic SessionEnding callback installed")
        return True
    except Exception:
        log.warning(
            "[win_shutdown] generic SessionEnding subscription failed",
            exc_info=True,
        )
        return False


def _install_session_ending_handler(
    window: "webview.Window",
    *,
    set_quitting: Optional[Callable[[], None]],
) -> None:
    """Subscribe to SystemEvents.SessionEnding → hard-exit.

    SystemEvents fires this on ``WM_QUERYENDSESSION``, before Windows
    starts terminating processes — our chance to get out cleanly before
    ``WM_CLOSE`` drives pywebview's FormClosed teardown (which throws a
    .NET exception that crashes pythonnet's exception marshaller). The
    handler flips ``set_quitting`` and then ``os._exit(0)``s rather than
    draining the message loop, so the crashing teardown never runs.

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

    def _on_session_ending(sender, args) -> None:
        # ``args.Reason`` is SessionEndReasons.Logoff or .SystemShutdown.
        # We treat both identically: hard-exit before pywebview's FormClosed
        # teardown can run.
        reason = "unknown"
        try:
            reason = str(args.Reason)
        except Exception:
            pass
        log.warning(
            "[win_shutdown] SessionEnding fired (reason=%s) — hard-exiting "
            "before pywebview's FormClosed teardown can crash pythonnet",
            reason,
        )
        if set_quitting is not None:
            try:
                set_quitting()
            except Exception:
                log.warning(
                    "[win_shutdown] set_quitting callback raised", exc_info=True
                )
        # Hard-exit immediately rather than draining the WinForms message loop
        # via ``safe_quit_window``. The FormClosed teardown (Application.Exit
        # recursion, clear_user_data, detached WebView2 RCW) throws a .NET
        # exception that pythonnet then crashes while *marshalling* back into
        # Python — ``System.NullReferenceException`` in
        # ``TypeManager.AllocateTypeObject``, an unhandled managed exception
        # (0xe0434352) that surfaces as the WerFault ".NET Framework / stopped
        # working" dialog. That crash is BELOW our Python ``on_close`` swallow
        # and the WinForms ``ThreadException`` net, so neither can catch it. The
        # old "post WM_QUIT + 2 s hard-exit timer" still ran ``safe_quit_window``
        # first, leaving a 2 s window for the same crash during OS shutdown.
        # This is a stateless GUI subprocess (settings persist on-change) and
        # the OS is reclaiming everything anyway — mirror
        # ``SettingsWindow._dispatch_quit`` and skip the teardown entirely.
        # No explicit log flush: handlers flush per record, and a flush could
        # block on a stuck stream — at SessionEnding the exit must never block.
        os._exit(0)

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
