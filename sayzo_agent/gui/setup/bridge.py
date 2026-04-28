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
import os
import sys
import threading
from typing import TYPE_CHECKING, Any

from sayzo_agent.config import Config
from sayzo_agent.gui.common import hotkey as hotkey_helpers
from sayzo_agent.gui.common.login import LoginCoordinator
from sayzo_agent.gui.setup.detect import detect_setup

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)

# Marker file written when the user completes the full first-run flow. Must
# match _PERMISSIONS_MARKER_NAME in detect.py. Kept for back-compat with
# detect.py's gate logic; the name is historical, it now signals "user
# completed the whole setup", not just the permissions step.
_PERMISSIONS_MARKER_NAME = ".permissions_onboarded_v1"


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
        self._login = LoginCoordinator(
            cfg, self._push_event, thread_name="setup-login",
        )

    # ------------------------------------------------------------------
    # Lifecycle (called from SetupWindow, not from JS). The setter is
    # underscore-prefixed so pywebview's method discovery doesn't expose
    # it as window.pywebview.api.attach_window — keeps the JS surface clean
    # and sidesteps a macOS Cocoa-backend readiness edge case. `result` is
    # a @property descriptor which pywebview skips anyway.
    # ------------------------------------------------------------------

    def _attach_window(self, window: "webview.Window") -> None:
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

        Returns immediately. Frontend listens for these events on
        ``window.sayzoEvents``:

        - ``login_url``      — URL the browser was directed to; UI uses
                               it to populate a "Copy URL" fallback.
        - ``login_tick``     — periodic ``{seconds_remaining: N}`` ticks
                               so the UI can render a countdown.
        - ``login_done``     — success; tokens saved.
        - ``login_error``    — failure with ``message``.
        - ``login_cancelled`` — user (or a superseding start_login)
                                cancelled via cancel_login.

        Safe to call again while a previous flow is pending — the
        earlier attempt is cancelled first so state doesn't clash.
        """
        self._login.start()
        return {"started": True}

    def cancel_login(self) -> dict[str, Any]:
        """Cancel an in-flight PKCE login. Emits ``login_cancelled`` when
        the worker observes the flag. No-op if nothing is pending."""
        return {"cancelled": self._login.cancel()}

    def start_model_download(self) -> dict[str, Any]:
        """Kick off the LLM weights download on a worker thread.

        Returns immediately. Frontend listens for ``download_progress`` and
        ``download_done`` / ``download_error`` events.
        """
        threading.Thread(
            target=self._download_worker, name="setup-download", daemon=True
        ).start()
        return {"started": True}

    # ---- Permissions (new) -------------------------------------------

    def prompt_mic_permission(self) -> dict[str, Any]:
        """User clicked Grant on the Microphone row. Fires the macOS TCC
        Microphone dialog on first call (subsequent calls are silent)."""
        if sys.platform != "darwin":
            return {"granted": None}
        from sayzo_agent.gui.setup import mac_permissions

        return {"granted": mac_permissions.prompt_microphone()}

    def prompt_audio_capture_permission(self) -> dict[str, Any]:
        """User clicked Grant on the Audio Capture row. Fires the macOS
        Audio Capture TCC dialog on first call."""
        if sys.platform != "darwin":
            return {"granted": None}
        from sayzo_agent.gui.setup import mac_permissions

        return {"granted": mac_permissions.prompt_audio_capture()}

    def prompt_notification_permission(self) -> dict[str, Any]:
        """User clicked Grant on the Notifications row. On macOS, fires the
        UNUserNotificationCenter dialog on first call. On Windows, just
        returns current toast-authorization status (non-prompting)."""
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            return {"granted": mac_permissions.prompt_notifications()}
        if sys.platform == "win32":
            from sayzo_agent.gui.setup import win_permissions

            return {"granted": win_permissions.has_notification_permission()}
        return {"granted": None}

    def open_mic_settings(self) -> None:
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            mac_permissions.open_mic_settings()

    def open_audio_capture_settings(self) -> None:
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            mac_permissions.open_audio_capture_settings()

    def open_notification_settings(self) -> None:
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            mac_permissions.open_notification_settings()
        elif sys.platform == "win32":
            from sayzo_agent.gui.setup import win_permissions

            win_permissions.open_notification_settings()

    # ---- Accessibility (macOS — needed for global hotkey + AX-based
    # web meeting detection) -----------------------------------------

    def open_accessibility_settings(self) -> dict[str, Any]:
        """Deep-link into System Settings → Privacy & Security → Accessibility.

        macOS has no programmatic grant for Accessibility — the user must
        click the + button under the list and add Sayzo manually. We
        return ``{"opened": True}`` on a best-effort spawn,
        ``{"opened": False}`` otherwise, so the frontend can flip its
        state accordingly.
        """
        if sys.platform != "darwin":
            return {"opened": False}
        from sayzo_agent.gui.setup import mac_permissions

        try:
            mac_permissions.open_accessibility_settings()
            return {"opened": True}
        except OSError as e:
            log.warning("failed to open Accessibility settings: %s", e)
            return {"opened": False}

    def check_accessibility_trusted(self) -> dict[str, Any]:
        """Return whether Sayzo currently has Accessibility permission.

        Polled by the setup window after deep-linking the user to System
        Settings. Wraps ``AXIsProcessTrustedWithOptions`` with an explicit
        no-prompt options dict — cheap and never prompts. macOS does not
        always update the trust bit for an already-running process even
        after the user grants access through System Settings; the Accessibility
        screen pairs this with a Restart escape hatch (see ``restart_app``)
        so the user is never stuck. Always ``{"trusted": False}`` on
        non-darwin.
        """
        if sys.platform != "darwin":
            return {"trusted": False}
        from sayzo_agent.gui.setup import mac_permissions

        return {"trusted": mac_permissions.is_accessibility_trusted()}

    # ---- Hotkey (persisted to user_settings.json) -------------------

    def get_hotkey(self) -> dict[str, Any]:
        return hotkey_helpers.get_hotkey(self._cfg)

    def validate_hotkey(self, binding: str) -> dict[str, Any]:
        return hotkey_helpers.validate_hotkey(binding)

    def save_hotkey(self, binding: str) -> dict[str, Any]:
        return hotkey_helpers.save_hotkey(self._cfg, binding)

    # ---- Setup-completion marker ------------------------------------

    def mark_permissions_onboarded(self) -> None:
        """Record that the user has reached the end of the first-run flow.

        Written by the Done screen just before ``finish()``. The name is
        historical — detect.py still uses ``has_permissions_onboarded`` as
        the gate for "should we re-open the setup window next launch".
        """
        path = self._cfg.data_dir / _PERMISSIONS_MARKER_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except OSError:
            log.warning(
                "failed to write permissions-onboarded marker at %s",
                path,
                exc_info=True,
            )

    def finish(self) -> None:
        """User clicked the success/done button. Setup is considered complete."""
        self._result = SetupResult.COMPLETED
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.warning("failed to destroy setup window", exc_info=True)

    def quit_app(self) -> None:
        """User cancelled. Service should exit without starting tray/agent.

        On macOS we ``os._exit`` directly rather than relying on
        ``window.destroy`` + ``webview.start`` returning, because pywebview's
        Cocoa backend calls ``NSApplication.stop_()`` in its windowWillClose_
        handler, and Apple's docs are explicit: ``stop:`` only takes effect
        after the *next* NSEvent is received. With no further user input
        after the cancel click, the NSApp runloop sits idle forever and the
        Python main thread never returns from ``webview.start``. The dock
        icon stays, Activity Monitor shows the process as not responding,
        the user has to force-quit. Hard-exiting from the bridge thread
        sidesteps the whole runloop. On Windows the WinForms backend doesn't
        have this quirk — Windows message loops exit naturally on form close
        — so the same hard exit there is just a safety belt.

        Reference: pywebview cocoa.py windowWillClose_ at
        site-packages/webview/platforms/cocoa.py:98 and Apple's
        NSApplication.stop(_:) documentation.
        """
        self._result = SetupResult.QUIT
        self._login.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.debug("failed to destroy setup window", exc_info=True)
        os._exit(0)

    def restart_app(self) -> None:
        """Relaunch Sayzo. Escape hatch for the Accessibility step.

        macOS doesn't reliably update the Accessibility trust bit for an
        already-running process when the user adds it through System
        Settings, so the polling check can stay False even after a real
        grant. A fresh process always reads the correct bit on startup.

        On macOS we spawn ``open -n`` against the .app bundle (detached) so
        a new instance starts before this one exits. On Windows / dev, we
        only exit — the user relaunches manually. Either way we hard-exit
        so pywebview/NSApp can't wedge the dying process.
        """
        from pathlib import Path

        self._result = SetupResult.QUIT
        self._login.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.debug(
                    "failed to destroy setup window before restart",
                    exc_info=True,
                )

        if sys.platform == "darwin":
            try:
                exe = Path(sys.executable).resolve()
                app_bundle = next(
                    (p for p in exe.parents if p.suffix == ".app"), None
                )
                if app_bundle is not None and app_bundle.exists():
                    import subprocess

                    subprocess.Popen(
                        ["open", "-n", str(app_bundle)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    log.warning("restart_app: relaunching %s", app_bundle)
                else:
                    log.warning(
                        "restart_app: no .app bundle found above %s — exiting "
                        "without relaunch",
                        exe,
                    )
            except Exception:
                log.warning("restart_app: relaunch spawn failed", exc_info=True)

        os._exit(0)

    # ------------------------------------------------------------------
    # Worker-thread implementations
    # ------------------------------------------------------------------

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
