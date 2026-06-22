"""Qt-level OS-shutdown hooks for the HUD subprocess.

Why this exists
---------------

The Sayzo HUD runs in a PySide6 + QtWebEngine subprocess (v2.11 rewrite —
see memory ``project_custom_hud_shipped.md``). Until v2.16.0 it had zero
plumbing for OS-initiated shutdown — no ``commitDataRequest`` handler, no
``aboutToQuit`` cleanup, no hard-exit backstop. Windows reported it in
the "Some apps are still preventing shutdown" dialog because Qt had no
contract to respond to ``WM_QUERYENDSESSION`` on. macOS would stall the
shutdown sequence (or force-kill after a timeout) for the same reason.

This module installs three layers — see plan ``zesty-zooming-taco.md``
v2.16.0 section for the full root-cause analysis:

* :func:`_on_commit_data` — connected with ``Qt.DirectConnection`` so the
  OS gets a synchronous acknowledgement on the emitting thread. This is
  the load-bearing fix. Qt translates Windows ``WM_QUERYENDSESSION`` and
  macOS ``applicationShouldTerminate:`` /
  ``NSWorkspaceWillPowerOffNotification`` into the same signal, so one
  handler covers both platforms.
* :func:`_make_about_to_quit_handler` — connected to ``aboutToQuit`` for
  explicit ``QWebEngineView`` + ``QWebEngineProfile`` teardown. Qt docs
  state that disk-based profiles must be destroyed before app exit or
  the HTTP cache may not flush cleanly. Best-effort, swallowed on
  exception.
* :func:`_arm_hud_hard_exit_timer` — a ``threading.Timer`` armed inside
  the commitDataRequest handler that calls ``os._exit(0)`` after a
  short timeout. The HUD keeps a timer because its Qt event loop can be
  starved (QtWebEngine GPU hang, deadlocked Chromium IPC, runaway JS) and
  ``app.quit()`` is only *queued*. The Settings/Setup ``win_shutdown.py``
  SessionEnding path used to share this timer-then-quit pattern but as of
  v3.20.3 hard-exits **unconditionally** (no timer) — pywebview's teardown
  is the thing that crashes there, so it must never run. Daemon thread,
  doesn't block clean shutdown when the event loop drains fast.

All three are cross-platform via Qt's signal abstraction. No
platform-specific dispatch needed inside this module.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)


_HUD_HARD_EXIT_TIMEOUT_SECS = 2.0


def install_qt_shutdown_hooks(
    app: "QApplication",
    *,
    view_provider,
) -> None:
    """Wire Qt-level OS-shutdown hooks onto ``app``.

    ``view_provider`` is a zero-arg callable returning the current
    ``QWebEngineView`` (or ``None`` if it's already gone). Passed as a
    callable rather than a direct reference so the ``aboutToQuit``
    handler reads the latest value at fire time — important because the
    widget may be reparented or replaced between install and shutdown.

    Connects two signals:

    * ``QGuiApplication.commitDataRequest`` (via ``app`` — same instance
      on Windows / macOS / Linux) with ``Qt.DirectConnection`` so the
      OS gets a synchronous ack on the emitting thread.
    * ``QApplication.aboutToQuit`` for ``QWebEngineView`` teardown.

    Best-effort. Failure to import PySide6 internals (shouldn't happen
    in a HUD subprocess, since they were just used to make ``app``)
    logs a warning and returns; the HUD still works, it just remains
    vulnerable to the "preventing shutdown" report.
    """
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QGuiApplication  # noqa: F401 — type confirm
    except Exception:
        log.warning("[hud.shutdown_hooks] PySide6 imports failed", exc_info=True)
        return

    def _on_commit_data(session_manager) -> None:
        # Qt's contract: return immediately. No UI, no blocking I/O.
        # We log + arm the hard-exit backstop + queue app.quit() on the
        # next event-loop iteration via QTimer.singleShot(0, ...).
        log.warning(
            "[hud] commitDataRequest fired — OS-level shutdown signaled"
        )
        _arm_hud_hard_exit_timer()
        try:
            # Tell the session manager we don't need to interact with
            # the user. On Qt 6 this is the canonical no-UI ack.
            session_manager.setRestartHint(session_manager.RestartHint.RestartNever)
        except Exception:
            # Older Qt versions / platform variants don't expose this
            # API. The signal having returned at all is itself the ack.
            pass
        QTimer.singleShot(0, app.quit)

    app.commitDataRequest.connect(
        _on_commit_data, Qt.ConnectionType.DirectConnection
    )

    app.aboutToQuit.connect(_make_about_to_quit_handler(view_provider))

    log.info("[hud.shutdown_hooks] Qt shutdown hooks installed")


def _make_about_to_quit_handler(view_provider):
    """Build an aboutToQuit slot that explicitly tears WebEngine down."""

    def _on_about_to_quit() -> None:
        log.info("[hud] aboutToQuit fired — running WebEngine teardown")
        view: Optional["QWebEngineView"] = None
        try:
            view = view_provider()
        except Exception:
            log.warning("[hud] view_provider raised in aboutToQuit", exc_info=True)
        if view is not None:
            try:
                # Sever the page reference before destroying the view so
                # the profile can be torn down cleanly. Per Qt docs:
                # destroying a disk-based QWebEngineProfile before app
                # exit is required for the HTTP cache to flush.
                view.setPage(None)
                view.deleteLater()
            except Exception:
                log.warning(
                    "[hud] aboutToQuit view teardown failed", exc_info=True
                )
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile

            QWebEngineProfile.defaultProfile().clearHttpCache()
        except Exception:
            log.warning(
                "[hud] aboutToQuit profile flush failed", exc_info=True
            )

    return _on_about_to_quit


def _arm_hud_hard_exit_timer() -> None:
    """Schedule ``os._exit(0)`` after the hard-exit timeout.

    Daemon thread, doesn't block clean shutdown if Qt drains the event
    loop first; fires only when something hung. The OS reclaims our
    resources at process exit, so skipping the rest of Qt's teardown
    is the right tradeoff at SessionEnding time. (The Settings/Setup
    ``win_shutdown.py`` SessionEnding path hard-exits unconditionally
    rather than arming a timer — see this module's docstring.)
    """

    def _fire() -> None:
        log.warning(
            "[hud] hard-exit timer fired after %.1fs — forcing os._exit(0)",
            _HUD_HARD_EXIT_TIMEOUT_SECS,
        )
        os._exit(0)

    timer = threading.Timer(_HUD_HARD_EXIT_TIMEOUT_SECS, _fire)
    timer.daemon = True
    timer.start()
