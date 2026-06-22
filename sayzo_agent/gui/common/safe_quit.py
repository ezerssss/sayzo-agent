"""Tear-down helper that exits the WinForms message loop without closing the form.

Status (v3.20.3): no longer on the Windows quit path
----------------------------------------------------

The Windows tray-quit and SessionEnding paths now **hard-exit**
(``os._exit(0)``) instead of calling this helper — see
``SettingsWindow._dispatch_quit`` and ``win_shutdown.py``. Even the
``Application.ExitThread`` approach below still drives enough of the
WinForms/WebView2 teardown that, at shutdown, a .NET exception gets
thrown and pythonnet crashes while *marshalling* it back into the
finalizing interpreter (``System.NullReferenceException`` in
``TypeManager.AllocateTypeObject`` — a known pythonnet 3.x shutdown
race), surfacing as the WerFault ".NET Framework / stopped working"
dialog. That crash is below the Python ``try/except`` and the WinForms
``ThreadException`` net, so it can't be caught; the only fix is to not
run the teardown. ``safe_quit_window`` is therefore now used **only on
macOS / non-Windows** (where it falls through to ``window.destroy()`` —
NSWindow close doesn't recurse and there's no .NET). The win32
``_try_exit_thread`` branch is retained for reference but is not on any
live path. The original rationale below is kept for history.

Why we don't just call ``window.destroy()`` on shutdown
-------------------------------------------------------

Pywebview 5.4's ``BrowserView.BrowserForm.on_close`` (the ``FormClosed``
event handler) ends with::

    if len(BrowserView.instances) == 0:
        self.Invoke(Func[Type](_shutdown))

where ``_shutdown`` calls ``WinForms.Application.Exit()``. ``Application.
Exit()`` walks ``Application.OpenForms`` and re-fires ``FormClosed`` for
each open form via ``Form.RaiseFormClosedOnAppExit``. Our form is still
in that collection at this point — ``Form.Close()`` hasn't finished yet,
we're inside its ``OnFormClosed`` invocation — so ``on_close`` runs a
second time. The second call hits ``del BrowserView.instances[uid]``
and raises ``KeyError(uid)`` (typically ``'master'``), which escapes
through pythonnet's ``__System_Windows_Forms_FormClosedEventHandler-
Dispatcher`` into .NET's unhandled-exception JIT-debugging dialog. The
dialog blocks ``Form.Close()`` from completing, so the agent's 3 s
``settings_launcher.quit()`` timeout fires and the Settings subprocess
gets terminated forcefully — visible to the user as
``[settings] subprocess didn't quit in 3 s — terminating`` in
``agent.log`` and a JIT dialog on screen.

Reliable repro: open the idle Settings window, click Quit on the tray.

What this helper does instead
-----------------------------

Marshal ``WinForms.Application.ExitThread()`` onto the UI thread via
``Form.BeginInvoke``. ``ExitThread`` posts ``WM_QUIT`` directly to the
calling thread's message queue. The message loop processes ``WM_QUIT``
and exits — no ``FormClosed`` event fires, no ``on_close`` runs, no
recursion, no KeyError. ``app.Run()`` returns, ``webview.start()``
returns, the Settings ``run_blocking()`` returns, and the subprocess
exits with rc=0 well within the agent's 3 s timeout.

We can skip ``Form.Close()``'s per-form .NET cleanup (Hide, Dispose,
WebView2 teardown) because the process is exiting anyway — Windows
reclaims the form's resources at process exit. The trade-off: a sliver
more memory held until the OS reaps the process, in exchange for not
crashing on the way out.

This only matters on Windows. ``cocoa.py`` has a similar ``del`` in
``windowWillClose_``, but NSWindow's close path doesn't recurse the
way ``Application.Exit()`` does on WinForms, so ``destroy()`` works
fine there. Non-Windows paths fall back to the original ``destroy()``.

Why we don't monkey-patch ``BrowserView.BrowserForm.on_close``
--------------------------------------------------------------

We tried in v2.7.5. Wrapping ``on_close`` with an idempotency latch
worked in unit tests and the synthetic shutdown reproducer, but in
production it hung the idle Settings show / tab-switch path
(``evaluate_js`` never returned, Windows logged ``Hang type: Unknown``
in Event Viewer). Cause not pinpointed; reverted in v2.7.6. See
``project_pywebview_close_guard_reverted.md`` for the full ruled-out
list. Bypassing ``window.destroy()`` only on the quit path — which is
what this helper does — has a much smaller blast radius: it never
touches pywebview's classes, never imports ``webview.platforms.winforms``
at module load time, and only runs at shutdown.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)


def safe_quit_window(window: "webview.Window") -> None:
    """Terminate the WinForms message loop without firing ``FormClosed``.

    On Windows, marshals ``Application.ExitThread()`` to the UI thread.
    On macOS / other platforms, falls back to ``window.destroy()`` since
    the recursion bug is WinForms-specific.

    Best-effort: any failure logs and falls back to ``destroy()``.
    """
    if sys.platform == "win32" and _try_exit_thread(window):
        return

    try:
        window.destroy()
    except Exception:
        log.warning("[safe_quit] destroy() fallback failed", exc_info=True)


def _try_exit_thread(window: "webview.Window") -> bool:
    """Marshal ``Application.ExitThread`` to the UI thread. Returns True on success.

    All imports happen inside the function so this module stays cheap to
    import and doesn't trigger ``webview.platforms.winforms`` /
    pythonnet / WebView2 assembly loading at module-load time. By the
    time anyone calls us, pywebview is fully running and has already
    imported everything we need — these are cached lookups.
    """
    try:
        from webview.platforms.winforms import BrowserView
        import System.Windows.Forms as WinForms
        from System import Action
    except Exception:
        log.warning("[safe_quit] failed to import WinForms helpers", exc_info=True)
        return False

    bv = BrowserView.instances.get(window.uid)
    if bv is None:
        # No BrowserForm for this uid — pywebview already cleaned up,
        # or the window was never realized. Nothing to do here; let the
        # caller fall through to destroy() for symmetry.
        return False

    try:
        if bv.IsDisposed:
            return False
    except Exception:
        # Pythonnet attribute access can raise on a disposing form;
        # treat that as "already gone" and fall back.
        return False

    try:
        bv.BeginInvoke(Action(WinForms.Application.ExitThread))
        return True
    except Exception:
        log.warning("[safe_quit] BeginInvoke ExitThread failed", exc_info=True)
        return False
