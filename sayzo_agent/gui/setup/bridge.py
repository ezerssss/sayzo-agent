"""JS-callable Python API exposed to the first-run setup window.

All public methods are reachable from the webview frontend as
``window.pywebview.api.<method_name>``. They must return JSON-serializable
values (or ``None``). Long-running work spawns a background thread and
pushes events back to JS via ``window.evaluate_js`` so the bridge call
itself returns immediately.
"""
from __future__ import annotations

import enum
import logging
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any, Callable

from sayzo_agent.config import Config
from sayzo_agent.gui.setup.detect import detect_setup

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)

# Audio Capture deep link — there's no public sub-pane for the Audio Capture
# permission specifically, so we land the user on the general Privacy & Security
# screen and they scroll to find Sayzo.
_MAC_PRIVACY_DEEPLINK = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


class SetupResult(enum.Enum):
    """Outcome of the first-run setup window."""

    COMPLETED = "completed"
    QUIT = "quit"


class Bridge:
    """JS-side API. Constructed once per :class:`SetupWindow` lifetime."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # Default to QUIT so closing the window via X / Cmd-Q (without ever
        # clicking the Done button) is treated as user cancellation.
        self._result: SetupResult = SetupResult.QUIT
        self._window: "webview.Window | None" = None
        self._event_listeners: list[Callable[[dict[str, Any]], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle (called from SetupWindow, not from JS)
    # ------------------------------------------------------------------

    def attach_window(self, window: "webview.Window") -> None:
        self._window = window

    @property
    def result(self) -> SetupResult:
        return self._result

    def _push_event(self, event: dict[str, Any]) -> None:
        """Send an event to the frontend's window.sayzoEvents queue.

        Safe to call from worker threads — pywebview marshals
        ``evaluate_js`` onto the GUI thread internally.
        """
        if self._window is None:
            log.debug("dropping event %r: window not attached", event)
            return
        # Quote-safe JSON serialization, then push as a JS literal.
        import json
        payload = json.dumps(event)
        try:
            self._window.evaluate_js(
                f"window.sayzoEvents && window.sayzoEvents.push({payload})"
            )
        except Exception:
            log.warning("failed to push event to webview", exc_info=True)

    # ------------------------------------------------------------------
    # JS-callable methods
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        return detect_setup(self._cfg).to_dict()

    def get_config_snapshot(self) -> dict[str, Any]:
        """Non-secret config bits the GUI may want for display."""
        return {
            "platform": sys.platform,
            "model_filename": self._cfg.llm.filename,
            "model_repo": self._cfg.llm.repo_id,
            "auth_url": self._cfg.auth.auth_url,
        }

    def start_login(self) -> dict[str, Any]:
        """Kick off the PKCE login flow on a worker thread.

        Returns immediately. Frontend listens for ``login_done`` /
        ``login_error`` events on ``window.sayzoEvents``.
        """
        threading.Thread(
            target=self._login_worker, name="setup-login", daemon=True
        ).start()
        return {"started": True}

    def start_model_download(self) -> dict[str, Any]:
        """Kick off the LLM weights download on a worker thread.

        Returns immediately. Frontend listens for ``download_progress`` and
        ``download_done`` / ``download_error`` events.
        """
        threading.Thread(
            target=self._download_worker, name="setup-download", daemon=True
        ).start()
        return {"started": True}

    def open_mac_privacy_settings(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            subprocess.Popen(["open", _MAC_PRIVACY_DEEPLINK])
        except OSError as e:
            log.warning("failed to open Privacy settings: %s", e)

    def recheck_mac_permission(self) -> dict[str, Any]:
        """Re-run the full detection. Returns updated status synchronously
        (cheap — bounded by the audio-tap probe timeout)."""
        return detect_setup(self._cfg).to_dict()

    def finish(self) -> None:
        """User clicked the success/done button. Setup is considered complete."""
        self._result = SetupResult.COMPLETED
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.warning("failed to destroy setup window", exc_info=True)

    def quit_app(self) -> None:
        """User cancelled. Service should exit without starting tray/agent."""
        self._result = SetupResult.QUIT
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.warning("failed to destroy setup window", exc_info=True)

    # ------------------------------------------------------------------
    # Worker-thread implementations
    # ------------------------------------------------------------------

    def _login_worker(self) -> None:
        import asyncio

        try:
            # Imported lazily so the bridge module can be imported in tests
            # without dragging in the full CLI module.
            from sayzo_agent.__main__ import _do_login

            asyncio.run(_do_login(self._cfg, quiet=True))
        except Exception as e:
            log.warning("login failed", exc_info=True)
            self._push_event({"type": "login_error", "message": str(e)})
            return
        self._push_event({"type": "login_done"})

    def _download_worker(self) -> None:
        import time

        from sayzo_agent.gui.setup.model_download import download_model_with_progress

        # Throttle progress events: at most every 100ms OR every 1% increment,
        # whichever comes first. Without this, a fast download floods
        # evaluate_js calls and the UI thread can't keep up.
        last_emit_ts = 0.0
        last_emit_pct = -1.0
        emit_interval_secs = 0.1

        def on_progress(done: int, total: int) -> None:
            nonlocal last_emit_ts, last_emit_pct
            now = time.monotonic()
            pct = (done / total * 100.0) if total > 0 else 0.0
            if (
                now - last_emit_ts >= emit_interval_secs
                or pct - last_emit_pct >= 1.0
                or (total > 0 and done >= total)  # always emit the final tick
            ):
                last_emit_ts = now
                last_emit_pct = pct
                self._push_event(
                    {"type": "download_progress", "done": done, "total": total}
                )

        try:
            path = download_model_with_progress(self._cfg, on_progress=on_progress)
        except Exception as e:
            log.warning("model download failed", exc_info=True)
            self._push_event({"type": "download_error", "message": str(e)})
            return
        self._push_event({"type": "download_done", "path": str(path)})
