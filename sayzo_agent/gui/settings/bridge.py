"""JS-callable Python API exposed to the Settings pywebview window.

Methods on this class are reachable from React as
``window.pywebview.api.<method_name>``. Long-running work delegates to a
helper that pushes events back to JS via ``window.evaluate_js`` so the
bridge call itself returns immediately.

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
from typing import TYPE_CHECKING, Any, Optional

from sayzo_agent import __version__, settings_store
from sayzo_agent.arm import seen_apps as _seen_apps
from sayzo_agent.config import Config, DetectorSpec, default_detector_specs
from sayzo_agent.gui.common import detectors as detector_helpers
from sayzo_agent.gui.common import hotkey as hotkey_helpers
from sayzo_agent.gui.common.login import LoginCoordinator
from sayzo_agent.gui.fs import open_folder
from sayzo_agent.gui.settings.ipc import IPCClient, IPCError, IPCNotConnected, Methods

if TYPE_CHECKING:
    import webview

log = logging.getLogger(__name__)

SUPPORT_URL = "https://sayzo.app/support"
WEBAPP_FALLBACK_URL = "https://sayzo.app"

# Per-platform permission row definitions. Empty list on Windows; macOS
# gets the four TCC-gated permissions Sayzo actually depends on. Driving
# the UI from data here means adding a future permission is one tuple,
# one dispatch branch in ``request_permission``, and (if not already in
# ``mac_permissions``) one helper.
_MAC_PERMISSION_ROWS: tuple[dict[str, str], ...] = (
    {
        "key": "mic",
        "label": "Microphone",
        "description": "Needed to hear your voice during meetings.",
    },
    {
        "key": "audio_capture",
        "label": "System Audio Recording",
        "description": "Needed to transcribe the other side of the conversation.",
    },
    {
        "key": "accessibility",
        "label": "Accessibility",
        "description": "Lets the global shortcut work when another app is focused.",
    },
)

# Direct deep-links for permissions that can only be toggled in System
# Settings (Accessibility has no programmatic grant flow). mic and
# audio_capture use the deep-links inside ``mac_permissions``.
_MAC_PERMISSION_DEEPLINKS: dict[str, str] = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Accessibility"
    ),
}

# Maps the wire-level notification key (used by React) to the nested
# ``settings_store`` patch and the live ``Config`` attribute path. Keeping
# the routing as data lets ``set_notification`` stay a single, validated
# method instead of a switch with one branch per flag.
_NOTIFICATION_KEYS: dict[str, dict] = {
    "master": {
        "store_patch": lambda v: {"notifications_enabled": v},
        "cfg_attr": ("notifications_enabled",),
    },
    "welcome": {
        "store_patch": lambda v: {"notify_welcome": v},
        "cfg_attr": ("notify_welcome",),
    },
    "post_arm": {
        "store_patch": lambda v: {"arm": {"notify_post_arm": v}},
        "cfg_attr": ("arm", "notify_post_arm"),
    },
    "capture_saved": {
        "store_patch": lambda v: {"notify_capture_saved": v},
        "cfg_attr": ("notify_capture_saved",),
    },
    "session_wrapped": {
        "store_patch": lambda v: {"arm": {"notify_session_wrapped": v}},
        "cfg_attr": ("arm", "notify_session_wrapped"),
    },
    "checkin": {
        "store_patch": lambda v: {"arm": {"checkin_enabled": v}},
        "cfg_attr": ("arm", "checkin_enabled"),
    },
    "meeting_ended_watcher": {
        "store_patch": lambda v: {"arm": {"meeting_ended_watcher_enabled": v}},
        "cfg_attr": ("arm", "meeting_ended_watcher_enabled"),
    },
    "confirm_hotkey_stop": {
        "store_patch": lambda v: {"arm": {"confirm_hotkey_stop": v}},
        "cfg_attr": ("arm", "confirm_hotkey_stop"),
    },
    # Daily-drill scheduler sub-toggle. Persists to user_settings.json
    # under "notifications.daily_drill_enabled"; the live agent is
    # nudged to reload via Methods.RELOAD_NOTIFICATION_CONFIG so the
    # change takes effect on the next scheduler tick.
    "daily_drill": {
        "store_patch": lambda v: {"notifications": {"daily_drill_enabled": v}},
        "cfg_attr": ("notifications", "daily_drill_enabled"),
    },
}


def _diagnostics_log_tail(logs_dir, max_lines: int = 200) -> str:
    """Read the tail of the agent's log file for the diagnostics blob.

    Returns a section header + the last N log lines (newest at the bottom).
    Best-effort: if the log file is missing or unreadable, return a stub
    explaining what was attempted so the support-channel reader can tell
    "no log available" apart from "the diagnostics shipped without it."
    """
    from pathlib import Path

    candidates = [Path(logs_dir) / "agent.log", Path(logs_dir) / "service.log"]
    for path in candidates:
        try:
            if not path.exists():
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = lines[-max_lines:]
            return f"--- log tail ({path.name}, last {len(tail)} of {len(lines)} lines) ---\n" + "".join(tail)
        except Exception:
            log.debug("[settings.bridge] log tail read failed for %s", path, exc_info=True)
            continue
    return f"--- log tail unavailable (looked in {logs_dir}) ---"


def _macos_update_diagnostics(max_lines: int = 200) -> str:
    """Three macOS-only diagnostics sections that disambiguate auto-update
    failures: bundle path (translocated vs normal), Spotlight scan for
    duplicate Sayzo bundles, and the apply_update.sh log tail.

    Returns an empty string on Windows / Linux so the caller can append
    unconditionally. Best-effort on every section — a missing or
    permission-denied subprocess / file produces an explanatory stub
    instead of raising so the rest of the diagnostics still ship.
    """
    if sys.platform != "darwin":
        return ""

    import subprocess
    from pathlib import Path

    sections: list[str] = []

    # 1. Resolved bundle path. Translocated installs show up as
    #    /private/var/folders/<random>/T/AppTranslocation/<uuid>/d/Sayzo.app
    #    (Apple TN2206 "macOS Code Signing In Depth"). A normal install
    #    is /Applications/Sayzo.app or ~/Applications/Sayzo.app. When the
    #    affected user's blob shows a translocation prefix, the bug is
    #    "user never dragged the DMG into /Applications" — rsync wrote
    #    into a read-only translocated copy and the original install
    #    stayed at vN.
    try:
        exe = Path(sys.executable).resolve()
        if len(exe.parents) >= 3 and exe.parents[2].suffix == ".app":
            bundle_path = str(exe.parents[2])
        else:
            bundle_path = f"<non-bundle>: {exe}"
    except Exception:
        bundle_path = "<failed to resolve>"
    sections.append(f"--- macOS bundle path ---\n{bundle_path}")

    # 2. mdfind multi-bundle scan. Catches the case where two installs
    #    coexist (/Applications + ~/Applications) and only one got
    #    rsync'd while LaunchServices / Dock launched the other.
    try:
        result = subprocess.run(
            ["mdfind", 'kMDItemCFBundleIdentifier == "com.sayzo.agent"'],
            capture_output=True, text=True, timeout=3.0,
        )
        if result.returncode == 0:
            paths = (result.stdout or "").strip() or "(none)"
            sections.append(f"--- macOS Sayzo bundles found ---\n{paths}")
        else:
            sections.append(
                f"--- macOS Sayzo bundles found ---\n"
                f"(mdfind exited {result.returncode}; Spotlight may be paused)"
            )
    except Exception:
        sections.append(
            "--- macOS Sayzo bundles found ---\n(mdfind unavailable)"
        )

    # 3. apply_update.log tail. Path is fixed in apply_update.sh:25-27 to
    #    $HOME/.sayzo/agent/logs/apply_update.log regardless of any
    #    SAYZO_DATA_DIR override (the shell helper can't see Python env
    #    overrides), so hardcoding the same path here matches what the
    #    swap helper actually wrote.
    log_path = Path.home() / ".sayzo" / "agent" / "logs" / "apply_update.log"
    try:
        if log_path.exists():
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = lines[-max_lines:]
            sections.append(
                f"--- apply_update.log tail (last {len(tail)} of {len(lines)} lines) ---\n"
                + "".join(tail).rstrip()
            )
        else:
            sections.append(
                f"--- apply_update.log not found at {log_path} ---\n"
                "(no auto-update has been attempted on this install)"
            )
    except Exception:
        log.debug("[settings.bridge] apply_update.log read failed", exc_info=True)
        sections.append(f"--- apply_update.log unreadable at {log_path} ---")

    return "\n\n".join(sections)


class Bridge:
    """JS-side API. Constructed once per :class:`SettingsWindow` lifetime."""

    def __init__(self, cfg: Config, *, initial_pane: Optional[str] = None) -> None:
        self._cfg = cfg
        self._initial_pane = initial_pane
        self._window: "Optional[webview.Window]" = None
        self._login = LoginCoordinator(
            cfg, self._push_event, thread_name="settings-login",
        )
        # Lazy connection to the live agent for state-mutating calls. When
        # the agent isn't running, every call_quiet returns None and the
        # wrapping bridge methods degrade to file-only behaviour.
        self._ipc = IPCClient(cfg.data_dir)

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

    def _push_update_phase(
        self,
        phase: str,
        *,
        version: Optional[str] = None,
        percent: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        """Emit an ``update_phase`` event with optional fields populated.

        Centralises the event shape so the wire contract with
        ``events.ts``'s ``update_phase`` union lives in one place — a typo
        in ``phase`` here is the only failure mode, vs. eight separate
        inline dicts where any ``type``/``phase`` key could drift.
        """
        event: dict[str, Any] = {"type": "update_phase", "phase": phase}
        if version is not None:
            event["version"] = version
        if percent is not None:
            event["percent"] = percent
        if message is not None:
            event["message"] = message
        self._push_event(event)

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

        Short env header + the tail of the agent log. The header is the
        minimum needed to identify "what version was running on what
        platform when this log was captured" — everything else lives in
        the log.

        React copies the returned ``text`` to the clipboard via the browser's
        ``navigator.clipboard`` API — Settings runs in pywebview's webview
        which exposes a working clipboard in both backends.
        """
        signed_in = self._has_tokens()
        header = "\n".join([
            f"Sayzo {__version__}",
            f"Platform:  {sys.platform} ({platform.platform()})",
            f"Python:    {sys.version.split()[0]}",
            f"Data dir:  {self._cfg.data_dir}",
            f"Captures:  {self._cfg.captures_dir}",
            f"Logs:      {self._cfg.logs_dir}",
            f"Signed in: {'yes' if signed_in else 'no'}",
        ])
        body = header + "\n\n" + _diagnostics_log_tail(self._cfg.logs_dir)
        macos_extra = _macos_update_diagnostics()
        if macos_extra:
            body += "\n\n" + macos_extra
        return {"text": body}

    # ------------------------------------------------------------------
    # JS-callable methods — Captures pane
    # ------------------------------------------------------------------

    def list_captures(self) -> list[dict[str, Any]]:
        """Return all known captures (in-progress + on-disk) for the
        Captures pane.

        Joins the live agent's processing state (via IPC) with the on-disk
        record.json files. Missing IPC = agent not running = just show
        what's on disk."""
        from sayzo_agent.captures_index import enumerate_captures, summary_to_dict

        processing: dict[str, dict] = {}
        try:
            result = self._ipc.call(Methods.SNAPSHOT_PROCESSING_CAPTURES)
            if isinstance(result, dict):
                processing = result
        except IPCNotConnected:
            pass
        except IPCError:
            log.debug("[settings.bridge] processing snapshot failed", exc_info=True)

        try:
            summaries = enumerate_captures(self._cfg.captures_dir, processing)
        except Exception:
            log.warning("[settings.bridge] enumerate_captures failed", exc_info=True)
            return []
        return [summary_to_dict(s) for s in summaries]

    def delete_capture(self, capture_id: str) -> dict[str, Any]:
        """Permanently delete the capture's local files. Validates the id
        shape to prevent path traversal."""
        from sayzo_agent.captures_index import delete_capture as _delete

        if not isinstance(capture_id, str):
            return {"deleted": False, "error": "invalid_id"}
        try:
            ok = _delete(self._cfg.captures_dir, capture_id)
        except ValueError:
            return {"deleted": False, "error": "invalid_id"}
        except Exception as exc:
            log.warning("[settings.bridge] delete_capture failed", exc_info=True)
            return {"deleted": False, "error": str(exc)}
        return {"deleted": ok}

    def retry_capture_upload(self, capture_id: str) -> dict[str, Any]:
        """Mark a capture as immediately due for retry, then nudge the live
        agent's upload sweep so the user doesn't wait for the next tick."""
        from sayzo_agent.captures_index import request_retry

        if not isinstance(capture_id, str):
            return {"retrying": False, "error": "invalid_id"}
        try:
            ok = request_retry(self._cfg.captures_dir, capture_id)
        except ValueError:
            return {"retrying": False, "error": "invalid_id"}
        except Exception as exc:
            log.warning("[settings.bridge] retry_capture_upload failed", exc_info=True)
            return {"retrying": False, "error": str(exc)}
        if ok:
            self._ipc.call_quiet(Methods.NUDGE_UPLOAD_RETRY)
        return {"retrying": ok}

    def open_capture_folder(self, capture_id: str) -> dict[str, Any]:
        """Reveal a capture's folder in the OS file manager."""
        from sayzo_agent.captures_index import is_valid_id

        if not isinstance(capture_id, str) or not is_valid_id(capture_id):
            return {"opened": False, "error": "invalid_id"}
        rec_dir = self._cfg.captures_dir / capture_id
        if not rec_dir.exists():
            return {"opened": False, "error": "missing"}
        try:
            open_folder(rec_dir)
        except Exception as exc:
            log.warning("[settings.bridge] open_capture_folder failed", exc_info=True)
            return {"opened": False, "error": str(exc)}
        return {"opened": True}

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
        self._login.start()
        return {"started": True}

    def cancel_login(self) -> dict[str, Any]:
        return {"cancelled": self._login.cancel()}

    def sign_out(self) -> dict[str, Any]:
        """Delete the on-disk token file and nudge the live agent to drop
        its cached copy. The cache nudge is best-effort — when the agent
        isn't running there's no cache to invalidate, so a missing IPC
        connection is silent."""
        from sayzo_agent.auth.store import TokenStore
        try:
            TokenStore(self._cfg.auth_path).clear()
        except Exception:
            log.warning("[settings.bridge] sign_out failed", exc_info=True)
            return {"signed_out": False}
        self._ipc.call_quiet(Methods.INVALIDATE_TOKEN_CACHE)
        return {"signed_out": True}

    def quit_agent(self) -> dict[str, Any]:
        """Tell the agent to fully shut down — same path as tray Quit.

        Returns ``ok=False`` when the agent isn't reachable so the UI
        can fall back to ``window.close``.
        """
        try:
            self._ipc.call(Methods.QUIT_AGENT)
        except IPCNotConnected:
            log.info("[settings.bridge] quit_agent: agent not reachable")
            return {"ok": False}
        except IPCError as e:
            log.warning("[settings.bridge] quit_agent IPC error: %s", e)
            return {"ok": False}
        return {"ok": True}

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

    def install_update_now(self) -> dict[str, Any]:
        """Kick off the full Phase B apply flow on a worker thread.

        If a staged release matching the latest manifest is already on disk
        (the background poll usually beats the user to it), skips straight
        to "applying" — call ``QUIT_AGENT`` on the live agent, which routes
        through ``apply_staged_if_newer`` in the quit path.

        Otherwise: fetches the manifest, downloads the platform installer
        with SHA256 verify, then triggers the apply.

        Frontend subscribes to ``update_phase`` events with shape
        ``{phase: "downloading" | "applying" | "noop_already_latest" |
        "queued_for_restart" | "error", percent?: int, version?: str,
        message?: str}``.
        """
        threading.Thread(
            target=self._install_update_worker,
            name="settings-install-update",
            daemon=True,
        ).start()
        return {"started": True}

    # ------------------------------------------------------------------
    # JS-callable methods — Shortcut
    # ------------------------------------------------------------------

    def get_hotkey(self) -> dict[str, Any]:
        return hotkey_helpers.get_hotkey(self._cfg)

    def validate_hotkey(self, binding: str) -> dict[str, Any]:
        return hotkey_helpers.validate_hotkey(binding)

    def save_hotkey(self, binding: str) -> dict[str, Any]:
        """Persist + live-rebind. The disk save runs first; if it succeeds,
        we nudge the live ``ArmController`` over IPC so the new combo takes
        effect without a service restart. The IPC step is best-effort:
        when the agent isn't running, disk save alone is enough — the new
        binding is picked up on next agent boot. When the agent IS running
        and rebind fails (binding already in use, pynput rejection), that
        error overrides the disk-save success since the user expects the
        new combo to start working immediately."""
        result = hotkey_helpers.save_hotkey(self._cfg, binding)
        if result.get("error") is not None:
            return result
        try:
            ipc_result = self._ipc.call(Methods.REBIND_HOTKEY, binding=binding)
        except IPCNotConnected:
            return result
        except IPCError as e:
            return {"error": str(e)}
        if isinstance(ipc_result, dict) and ipc_result.get("error"):
            return {"error": ipc_result["error"]}
        return result

    # ------------------------------------------------------------------
    # JS-callable methods — Notifications
    # ------------------------------------------------------------------

    def get_notifications(self) -> dict[str, bool]:
        """Read all notification flags from the live ``Config`` overlay."""
        return {
            "master": bool(self._cfg.notifications_enabled),
            "welcome": bool(self._cfg.notify_welcome),
            "post_arm": bool(self._cfg.arm.notify_post_arm),
            "capture_saved": bool(self._cfg.notify_capture_saved),
            "session_wrapped": bool(self._cfg.arm.notify_session_wrapped),
            "checkin": bool(self._cfg.arm.checkin_enabled),
            "meeting_ended_watcher": bool(self._cfg.arm.meeting_ended_watcher_enabled),
            "confirm_hotkey_stop": bool(self._cfg.arm.confirm_hotkey_stop),
            "daily_drill": bool(self._cfg.notifications.daily_drill_enabled),
        }

    def set_notification(self, key: str, value: bool) -> dict[str, Any]:
        """Persist a single notification flag.

        Mutates ``self._cfg`` so subsequent reads in this subprocess are
        consistent and writes the change to ``user_settings.json``. After
        a successful write, nudges the live agent (best-effort) to reload
        the relevant subsystem — daily_drill goes through
        ``RELOAD_NOTIFICATION_CONFIG`` so the scheduler picks the change
        up on its next tick.
        """
        spec = _NOTIFICATION_KEYS.get(key)
        if spec is None:
            return {"saved": False, "error": f"unknown notification key: {key}"}

        coerced = bool(value)
        attrs = spec["cfg_attr"]
        target: Any = self._cfg
        for a in attrs[:-1]:
            target = getattr(target, a)
        try:
            setattr(target, attrs[-1], coerced)
        except Exception:
            log.debug("[settings.bridge] cfg mutation failed for %s", key, exc_info=True)

        try:
            settings_store.save(self._cfg.data_dir, spec["store_patch"](coerced))
        except Exception:
            log.warning(
                "[settings.bridge] persist notification %s failed", key, exc_info=True,
            )
            return {"saved": False, "error": "couldn't write user_settings.json"}

        # Daily-drill scheduler has a dedicated reload IPC; master flag
        # routes through the same path so the scheduler picks up the
        # gate. The other toggles (welcome / capture_saved / arm.*)
        # are read from ``cfg`` on each event by the running agent;
        # the settings subprocess holds its own ``cfg`` copy, so those
        # changes take effect on the next agent process restart. Mid-
        # session live propagation for the arm flags is preexisting
        # tech debt (post_arm has the same shape).
        if key in ("daily_drill", "master"):
            try:
                self._ipc.call_quiet(Methods.RELOAD_NOTIFICATION_CONFIG)
            except Exception:
                log.debug(
                    "[settings.bridge] reload_notification_config nudge failed",
                    exc_info=True,
                )

        return {"saved": True}

    # ------------------------------------------------------------------
    # JS-callable methods — Recording
    # ------------------------------------------------------------------

    def get_recording_settings(self) -> dict[str, bool]:
        """Read recording-pane toggle state from the live ``Config`` overlay.

        ``per_app_capture`` is the inverse-mapped view of
        ``cfg.capture.system_scope``: True ⇒ ``arm_app`` (per-app capture
        enabled), False ⇒ ``endpoint`` (whole-system capture). The Settings
        pane phrases it as opt-in to per-app to match the beta framing.

        ``aec_enabled`` mirrors ``cfg.aec.enabled`` directly (WebRTC AEC3
        pre-pass, see sayzo_agent/aec.py).
        """
        return {
            "per_app_capture": self._cfg.capture.system_scope == "arm_app",
            "aec_enabled": bool(self._cfg.aec.enabled),
        }

    def set_recording_setting(self, key: str, value: bool) -> dict[str, Any]:
        """Persist a single recording-pane toggle.

        Returns ``requires_restart=True`` for every recording key because
        both the capture pipeline (``SystemCapture``) and the AEC pre-pass
        (``aec`` module's lazy-loaded APM) bind their config at agent
        startup and aren't reconstructed between arms. Live reload would
        require restructuring the Agent lifecycle and is out of scope.
        """
        coerced = bool(value)

        if key == "per_app_capture":
            new_scope = "arm_app" if coerced else "endpoint"
            try:
                self._cfg.capture.system_scope = new_scope  # type: ignore[assignment]
            except Exception:
                log.debug(
                    "[settings.bridge] cfg.capture.system_scope mutation failed",
                    exc_info=True,
                )
            patch: dict[str, Any] = {"capture": {"system_scope": new_scope}}
        elif key == "aec_enabled":
            try:
                self._cfg.aec.enabled = coerced
            except Exception:
                log.debug(
                    "[settings.bridge] cfg.aec.enabled mutation failed",
                    exc_info=True,
                )
            patch = {"aec": {"enabled": coerced}}
        else:
            return {"saved": False, "error": f"unknown recording key: {key}"}

        try:
            settings_store.save(self._cfg.data_dir, patch)
        except Exception:
            log.warning(
                "[settings.bridge] persist recording setting %s failed",
                key, exc_info=True,
            )
            return {"saved": False, "error": "couldn't write user_settings.json"}

        return {"saved": True, "requires_restart": True}

    # ------------------------------------------------------------------
    # JS-callable methods — Permissions
    # ------------------------------------------------------------------

    def get_permissions(self) -> list[dict[str, str]]:
        """Per-platform permission rows. Empty list on Windows."""
        if sys.platform != "darwin":
            return []
        return [dict(row) for row in _MAC_PERMISSION_ROWS]

    def request_permission(self, key: str) -> dict[str, Any]:
        """Fire the macOS TCC prompt for ``key``.

        Mic + audio_capture perform a one-shot probe the OS intercepts to
        surface the dialog. Accessibility has no programmatic grant —
        ``request_permission`` returns ``granted=null`` and the React
        caller falls back to ``open_permission_settings``.
        """
        if sys.platform != "darwin":
            return {"granted": None}

        try:
            from sayzo_agent.gui.setup import mac_permissions
        except Exception:
            log.warning("[settings.bridge] mac_permissions import failed", exc_info=True)
            return {"granted": None}

        if key == "mic":
            result = mac_permissions.prompt_microphone()
            return {
                "granted": result.granted,
                "stale_tcc_likely": result.stale_tcc_likely,
            }
        if key == "audio_capture":
            result = mac_permissions.prompt_audio_capture()
            return {
                "granted": result.granted,
                "stale_tcc_likely": result.stale_tcc_likely,
            }
        return {"granted": None}

    def open_permission_settings(self, key: str) -> dict[str, bool]:
        """Open System Settings to the relevant Privacy & Security sub-pane.

        macOS-only. Returns ``{"opened": False}`` on Windows so React can
        treat the call as a no-op without a platform branch.
        """
        if sys.platform != "darwin":
            return {"opened": False}

        try:
            from sayzo_agent.gui.setup import mac_permissions
        except Exception:
            log.warning("[settings.bridge] mac_permissions import failed", exc_info=True)
            return {"opened": False}

        try:
            if key == "mic":
                mac_permissions.open_mic_settings()
            elif key == "audio_capture":
                mac_permissions.open_audio_capture_settings()
            elif key in _MAC_PERMISSION_DEEPLINKS:
                import subprocess as _sp
                _sp.Popen(["open", _MAC_PERMISSION_DEEPLINKS[key]])
            else:
                return {"opened": False}
        except Exception:
            log.warning(
                "[settings.bridge] open_permission_settings %s failed",
                key, exc_info=True,
            )
            return {"opened": False}
        return {"opened": True}

    # ------------------------------------------------------------------
    # JS-callable methods — Meeting Apps
    # ------------------------------------------------------------------

    def list_detectors(self) -> list[dict[str, Any]]:
        """Return every detector in stored order, serialised for React.

        Reads from ``self._cfg.arm.detectors`` (the in-process snapshot
        loaded at subprocess start). Reads stay local — only mutations
        nudge the live agent — so this is fast enough to call on every
        pane render without polling concerns.
        """
        out: list[dict[str, Any]] = []
        for spec in self._cfg.arm.detectors:
            out.append(self._serialize_spec(spec))
        return out

    def toggle_detector(self, app_key: str, enabled: bool) -> dict[str, Any]:
        """Flip a detector's ``disabled`` flag in place + persist."""
        target = self._find_spec(app_key)
        if target is None:
            return {"saved": False, "error": f"unknown detector: {app_key}"}
        target.disabled = not bool(enabled)
        return self._persist_and_nudge()

    def remove_detector(self, app_key: str) -> dict[str, Any]:
        """Drop a detector from the list + persist."""
        before = len(self._cfg.arm.detectors)
        self._cfg.arm.detectors = [
            s for s in self._cfg.arm.detectors if s.app_key != app_key
        ]
        if len(self._cfg.arm.detectors) == before:
            return {"removed": False, "error": f"unknown detector: {app_key}"}
        return self._persist_and_nudge(extra={"removed": True})

    def add_detector(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Append a new detector built from ``spec``.

        ``spec`` is a JSON-friendly DetectorSpec shape (the React side
        constructs it from the Add-app dialog). Replaces any existing
        spec with the same ``app_key`` so a re-add doesn't duplicate.

        Browser specs that arrive with only ``url_patterns`` get a
        title pattern auto-derived from the first URL pattern's host.
        Required for macOS where ``get_browser_window_urls`` returns
        ``[]`` (TCC-avoidance) — without a title pattern, the spec
        would never match a tab on Mac.
        """
        if not isinstance(spec, dict):
            return {"added": False, "error": "spec must be an object"}
        try:
            new_spec = DetectorSpec.model_validate(spec)
        except Exception as e:
            return {"added": False, "error": f"invalid spec: {e}"}
        if (
            new_spec.is_browser
            and new_spec.url_patterns
            and not new_spec.title_patterns
        ):
            host = detector_helpers.host_from_url_pattern(new_spec.url_patterns[0])
            if host is not None:
                derived = detector_helpers.title_pattern_from_host(host)
                if derived is not None:
                    new_spec = new_spec.model_copy(
                        update={"title_patterns": [derived]}
                    )
        self._cfg.arm.detectors = [
            s for s in self._cfg.arm.detectors if s.app_key != new_spec.app_key
        ]
        self._cfg.arm.detectors.append(new_spec)
        # If the user previously dismissed this app from the suggested
        # list, undo that — they've changed their mind.
        for k in (*new_spec.process_names, *new_spec.bundle_ids):
            try:
                _seen_apps.undismiss(self._cfg.data_dir, k)
            except Exception:
                log.debug("[settings.bridge] undismiss failed", exc_info=True)
        return self._persist_and_nudge(extra={"added": True})

    def reset_detectors(self) -> dict[str, Any]:
        """Clear the user override so the shipping list reappears.

        Drops the ``arm.detectors`` key from ``user_settings.json`` so
        ``load_config`` (next agent boot) and ``ArmController.reload_detectors``
        (right now) both fall back to ``default_detector_specs``. Uses
        ``settings_store.replace`` rather than ``save`` because merge
        semantics can't represent a deletion — a missing key in the patch
        means "preserve", not "delete".
        """
        self._cfg.arm.detectors = default_detector_specs()
        try:
            current = settings_store.load(self._cfg.data_dir)
        except Exception:
            current = {}
        arm_block = current.get("arm") if isinstance(current.get("arm"), dict) else {}
        if isinstance(arm_block, dict) and "detectors" in arm_block:
            arm_block.pop("detectors", None)
            current["arm"] = arm_block
            try:
                settings_store.replace(self._cfg.data_dir, current)
            except Exception:
                log.warning("[settings.bridge] reset_detectors persist failed", exc_info=True)
                return {"reset": False, "error": "couldn't write user_settings.json"}
        # Nudge the agent regardless: even if no key was on disk, the
        # in-process cfg may have been mutated and the agent's notion of
        # the list could now be stale.
        self._ipc.call_quiet(Methods.RELOAD_DETECTORS)
        return {"reset": True}

    def list_seen_apps(self) -> list[dict[str, Any]]:
        """Return the suggested-to-add list (mic-holders Sayzo has seen
        but aren't on the whitelist yet).

        The on-disk file is scrubbed against the current detector list
        on read, so any app that was already added via this dialog won't
        re-appear here.
        """
        try:
            seen = _seen_apps.load(self._cfg.data_dir, self._cfg.arm.detectors)
        except Exception:
            log.debug("[settings.bridge] seen_apps.load failed", exc_info=True)
            return []
        return [
            {
                "key": s.key,
                "display_name": s.display_name,
                "process_name": s.process_name,
                "bundle_id": s.bundle_id,
            }
            for s in seen
        ]

    def dismiss_seen_app(self, app_key: str) -> dict[str, Any]:
        """Permanently dismiss a suggestion so it doesn't bubble up again."""
        try:
            _seen_apps.dismiss(self._cfg.data_dir, app_key)
        except Exception:
            log.warning("[settings.bridge] seen_apps.dismiss failed", exc_info=True)
            return {"dismissed": False}
        return {"dismissed": True}

    def snapshot_mic_state(self) -> dict[str, Any]:
        """Live mic-holder snapshot, polled by the Add-app dialog.

        Round-trips to the live agent over IPC so the snapshot reflects
        what the agent's whitelist watcher would see right now. When the
        agent isn't running we degrade to an empty snapshot — the user
        sees the "no apps holding the mic" hint without a hard error.
        """
        try:
            result = self._ipc.call(Methods.SNAPSHOT_MIC_STATE)
        except IPCNotConnected:
            return {"holders": [], "active": False, "running_processes": []}
        except IPCError:
            log.debug("[settings.bridge] snapshot_mic_state failed", exc_info=True)
            return {"holders": [], "active": False, "running_processes": []}
        if not isinstance(result, dict):
            return {"holders": [], "active": False, "running_processes": []}
        return result

    def snapshot_foreground(self) -> dict[str, Any]:
        """Live foreground-info snapshot. macOS Add-app dialog uses this
        to pick up the bundle id of the frontmost app while the mic is
        active (the platform has no per-process mic attribution)."""
        try:
            result = self._ipc.call(Methods.SNAPSHOT_FOREGROUND)
        except IPCNotConnected:
            return {}
        except IPCError:
            log.debug("[settings.bridge] snapshot_foreground failed", exc_info=True)
            return {}
        if not isinstance(result, dict):
            return {}
        return result

    def parse_meeting_url(self, url: str) -> dict[str, Any]:
        """Validate + preview a pasted meeting URL.

        Returns the fields React needs to render the live preview card
        and pre-fill the display-name field. ``error`` is non-null when
        the URL can't be parsed; the dialog disables Submit until that
        clears.
        """
        if not isinstance(url, str):
            return {"error": "url must be a string"}
        parsed = detector_helpers.parse_meeting_url(url)
        if parsed is None:
            return {"error": "not_a_url"}
        host, path = parsed
        return {
            "error": None,
            "host": host,
            "path": path,
            "display_name": detector_helpers.display_name_from_host(host),
        }

    def build_url_pattern(self, host: str, path: str, strict: bool) -> dict[str, Any]:
        """Compose the regex stored on the spec from ``parse_meeting_url`` parts.

        Kept on the Python side so the regex shape stays a Python concern
        — React just sends the user's choices and gets back the spec
        fragment to embed in ``add_detector(spec)``.
        """
        if not isinstance(host, str) or not host:
            return {"error": "host required"}
        if strict and not path:
            return {"error": "strict_needs_path"}
        pattern = detector_helpers.url_pattern(host, path or "", strict=bool(strict))
        return {"error": None, "pattern": pattern}

    def make_app_key(self, seed: str) -> str:
        """Slugify ``seed`` against the current detector list.

        Public so the React Add-app flow can build a spec with a
        guaranteed-unique key without round-tripping back through the
        bridge twice (once for parsing, once for slug). Falls back to
        ``"custom"`` if the slug normalises to empty.
        """
        if not isinstance(seed, str):
            seed = ""
        taken = [d.app_key for d in self._cfg.arm.detectors]
        return detector_helpers.unique_app_key(seed, taken)

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

    @staticmethod
    def _serialize_spec(spec: DetectorSpec) -> dict[str, Any]:
        """Pydantic model → React-friendly dict.

        Adds two derived fields React doesn't want to recompute:
        ``kind`` (``"desktop"`` | ``"web"``, mirroring the section tab in
        the Meeting Apps pane) and ``detail`` (the muted second-line
        text — hostname for web specs, process / bundle list for desktop
        specs).
        """
        kind = "web" if spec.is_browser else "desktop"
        if spec.is_browser:
            if spec.url_patterns:
                detail = "Web · " + detector_helpers.friendly_url_pattern(spec.url_patterns[0])
            elif spec.title_patterns:
                detail = "Web · matches window titles"
            else:
                detail = "Web"
        else:
            # Show platform-appropriate identifiers in the muted detail
            # line: bundle ids on macOS (where ``.exe`` names are
            # meaningless to the user), executable names on Windows.
            # Falls back to the other side if the platform-preferred
            # list is empty (e.g. macOS-only FaceTime spec has only
            # bundle_ids regardless of platform).
            import sys
            primary = spec.bundle_ids if sys.platform == "darwin" else spec.process_names
            secondary = spec.process_names if sys.platform == "darwin" else spec.bundle_ids
            chosen = primary if primary else secondary
            detail = ("Desktop · " + ", ".join(chosen)) if chosen else "Desktop"
        return {
            "app_key": spec.app_key,
            "display_name": spec.display_name,
            "kind": kind,
            "detail": detail,
            "is_browser": spec.is_browser,
            "process_names": list(spec.process_names),
            "bundle_ids": list(spec.bundle_ids),
            "url_patterns": list(spec.url_patterns),
            "title_patterns": list(spec.title_patterns),
            "disabled": bool(spec.disabled),
        }

    def _find_spec(self, app_key: str) -> Optional[DetectorSpec]:
        for s in self._cfg.arm.detectors:
            if s.app_key == app_key:
                return s
        return None

    def _persist_and_nudge(self, *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Write the current detector list to ``user_settings.json`` and
        ask the live agent to reload.

        The disk write is the source of truth — even if the IPC nudge
        fails (agent not running) the change survives, and the agent
        picks it up on next boot via ``load_config``. When the agent IS
        running, the nudge keeps the live whitelist in sync without
        waiting for a service restart.
        """
        try:
            serialised = [d.model_dump() for d in self._cfg.arm.detectors]
            settings_store.save(
                self._cfg.data_dir, {"arm": {"detectors": serialised}},
            )
        except Exception:
            log.warning("[settings.bridge] persist detectors failed", exc_info=True)
            base: dict[str, Any] = {"saved": False, "error": "couldn't write user_settings.json"}
            if extra:
                base.update(extra)
            return base

        self._ipc.call_quiet(Methods.RELOAD_DETECTORS)
        out: dict[str, Any] = {"saved": True}
        if extra:
            out.update(extra)
        return out

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

    def _install_update_worker(self) -> None:
        """Worker thread for ``install_update_now``.

        Two flows depending on whether the background poll already staged a
        download for us:

        1. **Already staged for the latest version** -> push "applying" and
           fire ``QUIT_AGENT`` over IPC. The agent's quit-path picks up the
           staged update via ``apply_staged_if_newer`` and hands off to the
           platform installer / swap helper. We never see the new agent come
           up — this subprocess will be terminated by the apply path along
           with the agent.

        2. **Not staged (or stale stage)** -> fetch manifest, stream-download
           the platform installer with hash verify, then proceed to step 1.

        Errors at any step push ``update_phase: error`` and bail. Best-effort
        — auto-update must not crash the Settings subprocess.
        """
        import asyncio

        from sayzo_agent.update import check as _update_check, is_newer
        from sayzo_agent.update_stage import (
            clear_staged,
            download_and_stage,
            read_staged,
        )

        try:
            staged = read_staged(self._cfg.data_dir)

            # Step 1: do we already have what we need on disk? The check
            # query against the manifest is cheap, but if a freshly-staged
            # build is sitting there, skip the round-trip and apply.
            info = None
            need_download = True
            if staged is not None and is_newer(__version__, staged.version):
                # Run a quick manifest check to confirm the stage matches what
                # the server currently advertises — guards against the case
                # where a much newer release dropped while a stale stage sat
                # waiting on disk. If the manifest says we're past staged or
                # if the manifest is unreachable, we still proceed with what
                # we have (offline-friendly: an old stage you've already
                # downloaded is better than nothing).
                try:
                    info = asyncio.run(_update_check(__version__))
                except Exception:
                    log.debug("[settings.bridge] manifest check failed during install", exc_info=True)
                    info = None
                if info is None or info.version == staged.version:
                    need_download = False
                else:
                    # Manifest advertises something newer than what's staged
                    # — clear the stale stage and let the download path
                    # re-fetch.
                    log.info(
                        "[settings.bridge] staged v%s is older than manifest v%s; re-downloading",
                        staged.version, info.version,
                    )
                    clear_staged(self._cfg.data_dir)

            # Step 2: download path. Need either a fresh manifest fetch (we
            # didn't take the staged shortcut) or the manifest from the
            # consistency check above.
            if need_download:
                if info is None:
                    try:
                        info = asyncio.run(_update_check(__version__))
                    except Exception as e:
                        log.warning(
                            "[settings.bridge] update check failed during install",
                            exc_info=True,
                        )
                        self._push_update_phase("error", message=str(e))
                        return
                if info is None:
                    # Manifest says we're on the latest. Surface this rather
                    # than silently doing nothing — the user clicked Install.
                    self._push_update_phase("noop_already_latest")
                    return

                # Stream the download with a progress callback that emits
                # one event per ~5% so the UI can render a smooth bar.
                self._push_update_phase(
                    "downloading", version=info.version, percent=0,
                )

                # Dedup adjacent identical percents. update_stage's caller
                # currently fires at clean 5% increments so duplicates are
                # rare in practice — this is defense-in-depth so a future
                # change to the throttle (e.g. per-chunk emit) doesn't
                # flood the GUI thread with no-op evaluate_js round-trips.
                last_pct = 0

                def _on_progress(done: int, total: int) -> None:
                    nonlocal last_pct
                    if total <= 0:
                        return
                    pct = int(min(99, round(done / total * 100)))
                    if pct == last_pct:
                        return
                    last_pct = pct
                    self._push_update_phase(
                        "downloading", version=info.version, percent=pct,
                    )

                try:
                    staged = asyncio.run(
                        download_and_stage(
                            info, self._cfg.data_dir,
                            progress_callback=_on_progress,
                        )
                    )
                except Exception as e:
                    log.warning(
                        "[settings.bridge] download_and_stage raised", exc_info=True
                    )
                    self._push_update_phase("error", message=str(e))
                    return

                if staged is None:
                    # SHA mismatch / disk error / network failure. The stager
                    # logs the specific reason — we surface a generic message
                    # to the UI rather than leaking internals.
                    self._push_update_phase(
                        "error",
                        message="Couldn't download the update. Try again later.",
                    )
                    return

            # Step 3: apply. By here ``staged`` is guaranteed non-None.
            assert staged is not None
            self._push_update_phase("applying", version=staged.version)

            # Write the quit-apply intent flag BEFORE QUIT_AGENT so the
            # agent's quit path applies the stage. Without the flag, a
            # plain tray Quit no longer auto-installs (see update_apply.py).
            # If the agent is unreachable, the flag still sits on disk for
            # the boot-time apply path to catch on next launch — we surface
            # "queued_for_restart" in that case.
            try:
                from sayzo_agent.update_apply import set_quit_apply_intent
                set_quit_apply_intent(self._cfg.data_dir)
                self._ipc.call(Methods.QUIT_AGENT)
            except IPCNotConnected:
                self._push_update_phase(
                    "queued_for_restart", version=staged.version,
                )
                return
            except IPCError as e:
                log.warning(
                    "[settings.bridge] QUIT_AGENT failed during install: %s", e
                )
                self._push_update_phase("error", message=str(e))
                return
        except Exception as e:
            # Catch-all so a buggy code path doesn't crash the Settings
            # subprocess silently.
            log.warning(
                "[settings.bridge] install_update_now worker crashed", exc_info=True
            )
            self._push_update_phase("error", message=str(e))
