"""JS-callable Python API exposed to the Settings pywebview window.

Mirrors :mod:`sayzo_agent.gui.setup.bridge` in shape — methods on this class
are reachable from React as ``window.pywebview.api.<method_name>``. Long-
running work spawns a background thread and pushes events back to JS via
``window.evaluate_js`` so the bridge call itself returns immediately.

Settings runs in its own subprocess (see ``gui/settings/window.py``); this
bridge therefore reads token state, version constants, and config from disk
rather than from a live ``Agent`` instance. Methods that need to talk to
the running agent (e.g., hotkey rebinding for Phase 3, mic-holder snapshots
for Phase 4) will route through an IPC client added in those phases — for
now the Phase 1 surface (Account + About) is entirely local.
"""
from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from sayzo_agent import __version__
from sayzo_agent.config import Config
from sayzo_agent.gui.fs import open_folder

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)

SUPPORT_URL = "https://sayzo.app/support"
WEBAPP_FALLBACK_URL = "https://sayzo.app"


class _ActiveLogin:
    """Book-keeping for an in-flight PKCE login so a second start_login can
    cancel and supersede the first. Mirrors ``setup.bridge._ActiveLogin``."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.thread: Optional[threading.Thread] = None


class Bridge:
    """JS-side API. Constructed once per :class:`SettingsWindow` lifetime."""

    def __init__(self, cfg: Config, *, initial_pane: Optional[str] = None) -> None:
        self._cfg = cfg
        self._initial_pane = initial_pane
        self._window: "Optional[webview.Window]" = None
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._active_login: Optional[_ActiveLogin] = None
        self._active_login_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle (called from SettingsWindow, not from JS).
    # ------------------------------------------------------------------

    def _attach_window(self, window: "webview.Window") -> None:
        self._window = window

    def _push_event(self, event: dict[str, Any]) -> None:
        """Send an event to the frontend's window.sayzoEvents queue.

        Safe to call from worker threads — pywebview marshals
        ``evaluate_js`` onto the GUI thread internally.
        """
        if self._window is None:
            log.debug("[settings.bridge] dropping event %r: window not attached", event)
            return
        payload = json.dumps(event)
        try:
            self._window.evaluate_js(
                f"window.sayzoEvents && window.sayzoEvents.push({payload})"
            )
        except Exception:
            log.warning("[settings.bridge] failed to push event", exc_info=True)

    # ------------------------------------------------------------------
    # JS-callable methods — General
    # ------------------------------------------------------------------

    def get_initial_pane(self) -> Optional[str]:
        """The pane name passed via ``--pane`` on the CLI, if any.

        React reads this on first mount to honour deep-link requests like
        the auth-expiry toast that wants to land directly on Account.
        """
        return self._initial_pane

    def get_about_info(self) -> dict[str, Any]:
        """Static info for the About pane.

        Read-only — the captures and logs paths can change at runtime only
        via ``SAYZO_DATA_DIR`` env override, which requires a restart, so
        snapshotting at window-open time is safe.
        """
        webapp = self._cfg.auth.effective_server_url or WEBAPP_FALLBACK_URL
        return {
            "version": __version__,
            "platform": sys.platform,
            "platform_human": platform.platform(),
            "python_version": sys.version.split()[0],
            "captures_dir": str(self._cfg.captures_dir),
            "logs_dir": str(self._cfg.logs_dir),
            "data_dir": str(self._cfg.data_dir),
            "web_app_url": webapp,
            "support_url": SUPPORT_URL,
        }

    def open_captures_folder(self) -> None:
        try:
            self._cfg.captures_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("[settings.bridge] mkdir captures failed", exc_info=True)
        open_folder(self._cfg.captures_dir)

    def open_logs_folder(self) -> None:
        try:
            self._cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("[settings.bridge] mkdir logs failed", exc_info=True)
        open_folder(self._cfg.logs_dir)

    def open_url(self, url: str) -> None:
        """Open ``url`` in the user's default browser."""
        if not isinstance(url, str) or not url:
            return
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("[settings.bridge] webbrowser.open failed", exc_info=True)

    def get_diagnostics(self) -> dict[str, str]:
        """Diagnostic blob for the About pane's "Copy diagnostics" button.

        React copies the returned ``text`` to the clipboard via the browser's
        ``navigator.clipboard`` API — Settings runs in pywebview's webview
        which exposes a working clipboard in both backends.
        """
        signed_in = self._has_tokens()
        text = "\n".join([
            f"Sayzo Agent {__version__}",
            f"Platform:  {sys.platform} ({platform.platform()})",
            f"Python:    {sys.version.split()[0]}",
            f"Data dir:  {self._cfg.data_dir}",
            f"Captures:  {self._cfg.captures_dir}",
            f"Logs:      {self._cfg.logs_dir}",
            f"Signed in: {'yes' if signed_in else 'no'}",
        ])
        return {"text": text}

    # ------------------------------------------------------------------
    # JS-callable methods — Account
    # ------------------------------------------------------------------

    def account_status(self) -> dict[str, Any]:
        """Snapshot of the on-disk token state.

        Returns ``state="signed_in"`` plus a best-effort ``signed_in_since``
        derived from the token file's mtime, or ``state="signed_out"``.
        """
        if not self._has_tokens():
            return {"state": "signed_out"}

        signed_in_at = self._signed_in_at()
        return {
            "state": "signed_in",
            "signed_in_since": signed_in_at.isoformat() if signed_in_at else None,
            "server": self._cfg.auth.effective_server_url or "",
        }

    def start_login(self) -> dict[str, Any]:
        """Kick off the PKCE login flow on a worker thread.

        Returns immediately. Frontend listens for these events on
        ``window.sayzoEvents``: ``login_url``, ``login_tick``, ``login_done``,
        ``login_error``, ``login_cancelled``.

        Safe to call again while a previous flow is pending — the earlier
        attempt is cancelled first so state doesn't clash.
        """
        with self._active_login_lock:
            prior = self._active_login
            if prior is not None:
                prior.cancel_event.set()
            active = _ActiveLogin()
            self._active_login = active

        t = threading.Thread(
            target=self._login_worker,
            args=(active,),
            name="settings-login",
            daemon=True,
        )
        active.thread = t
        t.start()
        return {"started": True}

    def cancel_login(self) -> dict[str, Any]:
        with self._active_login_lock:
            active = self._active_login
        if active is None:
            return {"cancelled": False}
        active.cancel_event.set()
        return {"cancelled": True}

    def sign_out(self) -> dict[str, Any]:
        """Delete the on-disk token file.

        The live agent's ``TokenStore`` may still hold a cached copy of the
        old tokens until its next miss; the live agent is expected to pick
        up the change on its next ``get_valid_token`` call (Phase 2 will add
        an IPC nudge to invalidate the cache eagerly).
        """
        from sayzo_agent.auth.store import TokenStore
        try:
            TokenStore(self._cfg.auth_path).clear()
        except Exception:
            log.warning("[settings.bridge] sign_out failed", exc_info=True)
            return {"signed_out": False}
        return {"signed_out": True}

    # ------------------------------------------------------------------
    # JS-callable methods — About
    # ------------------------------------------------------------------

    def check_for_update(self) -> dict[str, Any]:
        """Kick off a manifest fetch on a worker thread.

        Frontend listens for an ``update_result`` event with shape
        ``{has_update: bool, version?: str, url?: str, notes?: str}`` or
        ``update_error`` with ``{message: str}``.
        """
        threading.Thread(
            target=self._update_check_worker,
            name="settings-update-check",
            daemon=True,
        ).start()
        return {"checking": True}

    # ------------------------------------------------------------------
    # JS-callable methods — Lifecycle
    # ------------------------------------------------------------------

    def finish(self) -> None:
        """Close the Settings window. Subprocess exits when pywebview returns."""
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.warning("[settings.bridge] destroy failed", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_tokens(self) -> bool:
        from sayzo_agent.auth.store import TokenStore
        try:
            return TokenStore(self._cfg.auth_path).has_tokens()
        except Exception:
            log.debug("[settings.bridge] TokenStore read failed", exc_info=True)
            return False

    def _signed_in_at(self) -> Optional[datetime]:
        path = self._cfg.auth_path
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Worker-thread implementations
    # ------------------------------------------------------------------

    def _login_worker(self, active: _ActiveLogin) -> None:
        import asyncio

        from sayzo_agent.auth.exceptions import AuthenticationCancelled

        def on_url(url: str) -> None:
            self._push_event({"type": "login_url", "url": url})

        def on_tick(secs: int) -> None:
            self._push_event({"type": "login_tick", "seconds_remaining": secs})

        try:
            from sayzo_agent.__main__ import _do_login

            asyncio.run(
                _do_login(
                    self._cfg,
                    quiet=True,
                    cancel_event=active.cancel_event,
                    on_url_ready=on_url,
                    on_tick=on_tick,
                )
            )
        except AuthenticationCancelled:
            log.info("[settings.bridge] login cancelled")
            self._push_event({"type": "login_cancelled"})
            return
        except Exception as e:
            log.warning("[settings.bridge] login failed", exc_info=True)
            self._push_event({"type": "login_error", "message": str(e)})
            return
        finally:
            with self._active_login_lock:
                if self._active_login is active:
                    self._active_login = None
        self._push_event({"type": "login_done"})

    def _update_check_worker(self) -> None:
        import asyncio

        from sayzo_agent.update import check as _update_check

        try:
            info = asyncio.run(_update_check(__version__))
        except Exception as e:
            log.warning("[settings.bridge] update check failed", exc_info=True)
            self._push_event({"type": "update_error", "message": str(e)})
            return

        if info is None:
            self._push_event({"type": "update_result", "has_update": False})
        else:
            self._push_event({
                "type": "update_result",
                "has_update": True,
                "version": info.version,
                "url": info.url,
                "notes": info.notes,
            })
