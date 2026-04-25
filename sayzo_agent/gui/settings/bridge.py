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
import webbrowser
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from sayzo_agent import __version__, settings_store
from sayzo_agent.config import Config
from sayzo_agent.gui.common import hotkey as hotkey_helpers
from sayzo_agent.gui.common.login import LoginCoordinator
from sayzo_agent.gui.fs import open_folder
from sayzo_agent.gui.settings.ipc import IPCClient, IPCError, IPCNotConnected

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
    {
        "key": "automation",
        "label": "Automation (browsers)",
        "description": "Lets Sayzo read the current tab's URL to detect web meetings.",
    },
)

# Direct deep-links for permissions that aren't grant-by-probe (Accessibility
# + Automation can only be toggled by the user in System Settings, no
# programmatic flow). ``mac_permissions`` already owns mic / audio-capture /
# notifications deep-links so we reuse those.
_MAC_PERMISSION_DEEPLINKS: dict[str, str] = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Accessibility"
    ),
    "automation": (
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Automation"
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
}


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
        self._ipc.call_quiet("invalidate_token_cache")
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
            ipc_result = self._ipc.call("rebind_hotkey", binding=binding)
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
        """Read the four notification flags from the live ``Config`` overlay."""
        return {
            "master": bool(self._cfg.notifications_enabled),
            "welcome": bool(self._cfg.notify_welcome),
            "post_arm": bool(self._cfg.arm.notify_post_arm),
            "capture_saved": bool(self._cfg.notify_capture_saved),
        }

    def set_notification(self, key: str, value: bool) -> dict[str, Any]:
        """Persist a single notification flag.

        Mutates ``self._cfg`` so subsequent reads in this subprocess are
        consistent and writes the change to ``user_settings.json``. The
        running agent process keeps its in-memory ``Config`` until restart;
        Phase 3's IPC layer will let us nudge it to reload sooner.
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
        return {"saved": True}

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
        surface the dialog. Accessibility + automation have no programmatic
        grant — ``request_permission`` returns ``granted=null`` and the
        React caller falls back to ``open_permission_settings``.
        """
        if sys.platform != "darwin":
            return {"granted": None}

        try:
            from sayzo_agent.gui.setup import mac_permissions
        except Exception:
            log.warning("[settings.bridge] mac_permissions import failed", exc_info=True)
            return {"granted": None}

        if key == "mic":
            return {"granted": mac_permissions.prompt_microphone()}
        if key == "audio_capture":
            return {"granted": mac_permissions.prompt_audio_capture()}
        if key == "notifications":
            return {"granted": mac_permissions.prompt_notifications()}
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
            elif key == "notifications":
                mac_permissions.open_notification_settings()
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
