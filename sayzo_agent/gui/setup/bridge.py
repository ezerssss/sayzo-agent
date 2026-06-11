"""JS-callable Python API exposed to the first-run setup window.

All public methods are reachable from the webview frontend as
``window.pywebview.api.<method_name>``. They must return JSON-serializable
values (or ``None``). Long-running work spawns a background thread and
pushes events back to JS via ``window.evaluate_js`` so the bridge call
itself returns immediately.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import sys
import webbrowser
from typing import TYPE_CHECKING, Any

from sayzo_agent.config import Config
from sayzo_agent.gui.common import hotkey as hotkey_helpers
from sayzo_agent.gui.common.login import LoginCoordinator
from sayzo_agent.gui.settings.ipc import IPCClient, Methods
from sayzo_agent.gui.setup.detect import detect_setup

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)

# Marker file written when the user completes the full first-run flow. Must
# match _PERMISSIONS_MARKER_NAME in detect.py. Kept for back-compat with
# detect.py's gate logic; the name is historical, it now signals "user
# completed the whole setup", not just the permissions step.
_PERMISSIONS_MARKER_NAME = ".permissions_onboarded_v1"

# One-shot marker written by restart_app() before it hard-exits. The next
# instance reads it on the first get_status() call, deletes it, and tells
# the frontend to skip straight back to the Accessibility screen — instead
# of the default sequence[2] (Microphone, step 3) that initialScreen() would
# otherwise return for a token+model-already-present user. Without this,
# clicking "Restart Sayzo" from the Accessibility-waiting state dropped the
# user three screens back even though every earlier permission was already
# done.
_RESUME_AT_ACCESSIBILITY_MARKER = ".resume_at_accessibility"


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
        # Lazy connection to the live agent. The setup window runs in its own
        # subprocess, so ``set_recording_indicator`` can't reach the running
        # agent's cfg by mutating ours — it nudges over IPC instead. Best-
        # effort: during first-run the service is often not up yet, in which
        # case the disk write alone suffices (picked up on the next boot).
        self._ipc = IPCClient(cfg.data_dir)

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
        status = detect_setup(self._cfg).to_dict()
        # One-shot — consumed by App.tsx's initialScreen() on first read after
        # a Restart-Sayzo round-trip from the Accessibility screen.
        status["resume_at"] = self._consume_resume_marker()
        return status

    def _consume_resume_marker(self) -> str | None:
        path = self._cfg.data_dir / _RESUME_AT_ACCESSIBILITY_MARKER
        if not path.exists():
            return None
        try:
            path.unlink()
        except OSError:
            log.warning(
                "failed to remove resume marker at %s", path, exc_info=True
            )
        return "accessibility"

    def get_config_snapshot(self) -> dict[str, Any]:
        """Non-secret config bits the GUI may want for display."""
        return {
            "platform": sys.platform,
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

    # ---- Permissions (new) -------------------------------------------

    def prompt_mic_permission(self) -> dict[str, Any]:
        """User clicked Grant on the Microphone row. Fires the macOS TCC
        Microphone dialog on first call (subsequent calls are silent).

        Returns ``{"granted": bool|None, "stale_tcc_likely": bool}``. The
        ``stale_tcc_likely`` flag is True when the helper fingerprinted a
        TCC entry from a previous Sayzo install with a different signing
        identity silently denying the request without ever presenting UI
        (sync read returned NotDetermined, request branch returned False
        in <500 ms). The frontend swaps the generic "blocked" copy for
        targeted recovery steps in that case — System Settings shows the
        toggle ON, which makes the generic message actively misleading.
        """
        if sys.platform != "darwin":
            return {"granted": None, "stale_tcc_likely": False}
        from sayzo_agent.gui.setup import mac_permissions

        result = mac_permissions.prompt_microphone()
        return {
            "granted": result.granted,
            "stale_tcc_likely": result.stale_tcc_likely,
        }

    def prompt_audio_capture_permission(self) -> dict[str, Any]:
        """User clicked Grant on the Audio Capture row. Fires the macOS
        Audio Capture TCC dialog on first call.

        Returns ``{"granted": bool|None, "stale_tcc_likely": bool}`` —
        same shape as the mic prompt for the same reason: a stale entry
        from a pre-v2.6.0 install silently denies, so the GUI needs the
        flag to swap "blocked" copy for "remove from System Settings,
        then retry" copy.
        """
        if sys.platform != "darwin":
            return {"granted": None, "stale_tcc_likely": False}
        from sayzo_agent.gui.setup import mac_permissions

        result = mac_permissions.prompt_audio_capture()
        return {
            "granted": result.granted,
            "stale_tcc_likely": result.stale_tcc_likely,
        }

    # Notification-permission bridge methods were removed in v2.10 along
    # with the Notifications onboarding screen — the custom HUD overlay
    # (`project_custom_hud_shipped`) doesn't depend on OS notification
    # permission. The corresponding TypeScript declarations were dropped
    # from `lib/bridge.ts`.

    def reset_mic_permission_and_restart(self) -> None:
        """Programmatic recovery for the macOS "silent-deny" mic state.

        Runs ``tccutil reset Microphone com.sayzo.agent`` to clear any
        orphan TCC entry (from a previous Sayzo install with a different
        signing identity, or from an earlier silent-deny that recorded
        a Denied entry into the database), then immediately relaunches
        the app and hard-exits this process. Apple's documented behavior
        is that an already-running process must be quit and restarted
        after a TCC reset before subsequent ``requestAccess`` calls
        will surface a fresh dialog — AVFoundation caches the
        NotDetermined→Denied transition per process.

        Why this exists at all: the previous "open System Settings and
        remove Sayzo" recovery copy was wrong for the most common case.
        An orphan CR-mismatched entry is FILTERED OUT of System Settings
        (the user opens the Microphone pane and Sayzo isn't there),
        leaving them with no actionable button to click. Bundle-level
        recovery via tccutil is the only path that works without
        Terminal.

        No-op on non-darwin.
        """
        if sys.platform != "darwin":
            return
        from sayzo_agent.gui.setup import mac_permissions

        mac_permissions._tccutil_reset_service(
            mac_permissions._TCC_SERVICE_MICROPHONE
        )
        # Relaunch will hard-exit. Window closing logic is unnecessary
        # because mac_permissions.relaunch_app calls os._exit(0).
        self._login.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.debug(
                    "failed to destroy setup window before mic-reset relaunch",
                    exc_info=True,
                )
        mac_permissions.relaunch_app()

    def reset_audio_capture_permission_and_restart(self) -> None:
        """Same recovery flow as :meth:`reset_mic_permission_and_restart`,
        for the Audio Capture (Process Taps) TCC service. Resets the
        ``AudioCapture`` service for ``com.sayzo.agent`` and relaunches.
        ``AudioCapture`` is the private TCC service name used by
        ``AudioHardwareCreateProcessTap`` (macOS 14.4+, see Apple's
        ``insidegui/AudioCap`` reference)."""
        if sys.platform != "darwin":
            return
        from sayzo_agent.gui.setup import mac_permissions

        mac_permissions._tccutil_reset_service(
            mac_permissions._TCC_SERVICE_AUDIO_CAPTURE
        )
        self._login.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                log.debug(
                    "failed to destroy setup window before audio-reset relaunch",
                    exc_info=True,
                )
        mac_permissions.relaunch_app()

    def get_tcc_diagnostic_text(self) -> dict[str, Any]:
        """Return a plain-text diagnostic summary the user can paste into
        a support thread when "Reset & Restart Sayzo" hasn't fixed the
        silent-deny.

        Includes bundle Info.plist key presence (the smoking gun for the
        AVFoundation pre-flight rejection failure mode), ``codesign -dvv``
        output (the smoking gun for the orphan-CR failure mode), and the
        last 50 ``[mac_permissions]`` / ``[mac_heal]`` lines from
        agent.log. macOS-specific — Windows / Linux callers get a short
        placeholder so the same UI works without a special case.
        """
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            return {
                "text": mac_permissions.gather_tcc_diagnostic_text(self._cfg)
            }
        return {
            "text": (
                f"Platform: {sys.platform} — TCC diagnostic is macOS-specific.\n"
            )
        }

    def copy_tcc_diagnostic_to_clipboard(self) -> dict[str, Any]:
        """Pipe the TCC diagnostic text into ``pbcopy`` so the user can
        paste it into a support thread with one keystroke. Returns
        ``{copied: bool}`` so the React button can flash a confirmation."""
        if sys.platform != "darwin":
            return {"copied": False}
        from sayzo_agent.gui.setup import mac_permissions

        return {
            "copied": mac_permissions.copy_diagnostic_to_clipboard(self._cfg)
        }

    def open_log_folder(self) -> dict[str, Any]:
        """Open the agent's log directory in Finder / Explorer so a
        stuck user can grab ``agent.log`` and email it to support
        without hunting through ``~/Library/Application Support`` or
        ``%APPDATA%``. Returns ``{opened: bool}`` for UI feedback.

        Cross-platform: ``open`` on macOS, ``explorer`` on Windows.
        """
        log_dir = self._cfg.logs_dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            log.debug("log_dir mkdir failed", exc_info=True)
        try:
            import subprocess as _sp

            if sys.platform == "darwin":
                _sp.Popen(
                    ["open", str(log_dir)],
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
            elif sys.platform == "win32":
                # `explorer <path>` returns rc=1 on success — quirk of
                # explorer.exe — so we don't check the return code.
                _sp.Popen(["explorer", str(log_dir)])
            else:
                return {"opened": False}
            return {"opened": True}
        except Exception:
            log.warning("open_log_folder failed", exc_info=True)
            return {"opened": False}

    def open_web_app(self) -> None:
        """Open the Sayzo web platform home in the user's browser.

        Surfaced from the onboarding Done screen so a new user can land on
        the platform right after install.
        """
        url = self._cfg.auth.effective_server_url or "https://sayzo.app"
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("open_web_app failed", exc_info=True)

    def open_mic_settings(self) -> None:
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            mac_permissions.open_mic_settings()

    def open_audio_capture_settings(self) -> None:
        if sys.platform == "darwin":
            from sayzo_agent.gui.setup import mac_permissions

            mac_permissions.open_audio_capture_settings()

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

    # ---- Recording indicator (HUD pill visibility) ------------------

    def get_recording_indicator(self) -> dict[str, Any]:
        """Read the current "show recording indicator" preference.

        The onboarding picker reads this on mount so a user who quits +
        re-opens setup after the first time lands on their previous pick
        instead of the default. Returns the live ``Config`` value so a
        ``set_recording_indicator`` earlier in the same session is
        reflected.
        """
        return {"visible": bool(self._cfg.hud.show_recording_indicator)}

    def set_recording_indicator(self, visible: bool) -> dict[str, Any]:
        """Persist the onboarding picker's choice + nudge the live agent.

        Writes the boolean to ``user_settings.json`` under ``hud`` and updates
        ``self._cfg.hud.show_recording_indicator`` so a later read in this
        same setup session is consistent. The setup window runs in its own
        subprocess, though, so mutating our cfg does NOT reach a concurrently
        running agent — we nudge it over IPC (``RELOAD_HUD_CONFIG``) so a
        "Stay out of the way" pick applies on the next arm rather than waiting
        for a restart. Best-effort: during first-run the service is usually
        not up yet, in which case ``call_quiet`` is silent and the on-disk
        value is read on the agent's next boot.
        """
        from sayzo_agent import settings_store

        coerced = bool(visible)
        try:
            settings_store.save(
                self._cfg.data_dir,
                {"hud": {"show_recording_indicator": coerced}},
            )
        except Exception:
            log.warning(
                "[bridge] persist hud.show_recording_indicator failed",
                exc_info=True,
            )
            return {"saved": False, "error": "couldn't write user_settings.json"}
        try:
            self._cfg.hud.show_recording_indicator = coerced
        except Exception:
            log.debug(
                "[bridge] cfg.hud.show_recording_indicator mutation failed",
                exc_info=True,
            )
        self._ipc.call_quiet(Methods.RELOAD_HUD_CONFIG)
        return {"saved": True}

    # ---- Web-onboarding gate ----------------------------------------

    def recheck_account_status(self) -> dict[str, Any]:
        """Re-fetch ``GET /api/me`` and return the updated SetupStatus.

        Called by the FinishSignup screen — both manually (the "I've
        finished" button) and via an 8 s auto-poll while the screen is
        visible. Also fires once after a successful PKCE login completes,
        so the React app routes to FinishSignup or to permissions based
        on a fresh server response rather than a stale cache.

        The fetch happens inline on this thread — pywebview already calls
        JS-callable bridge methods on a worker thread, so blocking briefly
        here doesn't freeze the UI. On any non-``ok`` outcome the cache is
        updated; on auth/transient failures the cache is left alone so a
        flaky network can't downgrade a previously-positive state.
        """
        from sayzo_agent.account import refresh_and_cache
        from sayzo_agent.auth.client import make_auth_client

        client = make_auth_client(self._cfg)
        if client is None:
            log.info(
                "[bridge.account] recheck: no auth client (signed-out or no server_url)"
            )
            return self._account_status_payload(fetch_status="auth_required")

        try:
            response = asyncio.run(refresh_and_cache(client, self._cfg))
        except Exception as exc:
            log.warning(
                "[bridge.account] recheck raised: %r", exc, exc_info=True
            )
            return self._account_status_payload(
                fetch_status="unknown_error", error=repr(exc)
            )

        return self._account_status_payload(
            fetch_status=response.status,
            onboarding_url=response.onboarding_url,
        )

    def open_onboarding_url(self) -> dict[str, Any]:
        """Open the web-onboarding URL from the cache in the default browser.

        Falls back to ``server_url + /onboarding`` if no cache exists yet
        (e.g. user clicked the button before the first recheck landed).
        Returns ``{"opened": bool, "url": <url-or-null>}`` so the frontend
        can render an inline copyable URL on failure.
        """
        url = self._resolve_onboarding_url(self._safe_read_cache())
        if not url:
            return {"opened": False, "url": None}
        try:
            opened = webbrowser.open(url, new=2)
        except Exception:
            log.warning(
                "[bridge.account] webbrowser.open failed for %s", url, exc_info=True
            )
            opened = False
        return {"opened": bool(opened), "url": url}

    def _safe_read_cache(self):
        from sayzo_agent.account import read_cache
        try:
            return read_cache(self._cfg)
        except Exception:
            log.warning("[bridge.account] cache read failed", exc_info=True)
            return None

    def _resolve_onboarding_url(self, cached) -> str | None:
        if cached is not None and cached.onboarding_url:
            return cached.onboarding_url
        base = self._cfg.auth.effective_server_url
        return (base.rstrip("/") + "/onboarding") if base else None

    def _account_status_payload(
        self,
        *,
        fetch_status: str,
        onboarding_url: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Combined response: fresh SetupStatus snapshot + the fetch outcome.

        The frontend uses ``fetch_status`` to drive the FinishSignup screen
        UX (e.g. show a retry button on transient_error, bounce to Welcome
        on auth_required) without having to re-call ``get_status()``.
        """
        cached = self._safe_read_cache()
        status = detect_setup(self._cfg).to_dict()
        status["resume_at"] = self._consume_resume_marker()
        return {
            "status": status,
            "fetch_status": fetch_status,
            "onboarding_url": onboarding_url or self._resolve_onboarding_url(cached),
            "error": error,
        }

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
        """User clicked the success/done button. Setup is complete.

        Must be invoked from a real user click — see ``quit_app`` for
        the macOS NSEvent quirk that breaks JS-initiated closes.
        """
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

        Writes ``.resume_at_accessibility`` before exit so the next instance
        jumps straight back to the Accessibility screen via App.tsx's
        initialScreen(). Without this, the new process treated the user as
        a fresh token+model-present startup and dropped them at sequence[2]
        (Microphone, step 3) — three screens behind where they were.
        """
        from pathlib import Path

        # Write the resume marker FIRST. Even if the relaunch spawn fails
        # below, the marker is harmless on a stale boot — get_status()
        # consumes it once and ignores it after that.
        try:
            marker = self._cfg.data_dir / _RESUME_AT_ACCESSIBILITY_MARKER
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except OSError:
            log.warning(
                "failed to write resume marker before restart", exc_info=True
            )

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

