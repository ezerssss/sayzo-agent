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
            f"Sayzo {__version__}",
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
        self._ipc.call_quiet(Methods.INVALIDATE_TOKEN_CACHE)
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
        """
        if not isinstance(spec, dict):
            return {"added": False, "error": "spec must be an object"}
        try:
            new_spec = DetectorSpec.model_validate(spec)
        except Exception as e:
            return {"added": False, "error": f"invalid spec: {e}"}
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
            bits: list[str] = []
            if spec.process_names:
                bits.append(", ".join(spec.process_names))
            elif spec.bundle_ids:
                bits.append(", ".join(spec.bundle_ids))
            detail = ("Desktop · " + bits[0]) if bits else "Desktop"
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
