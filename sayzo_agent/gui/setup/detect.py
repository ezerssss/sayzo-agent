"""Pure first-run setup detection.

No UI. No mutation. Called at service startup to decide whether to open the
first-run GUI window before launching the tray + capture pipeline.

The detection probes:
  - auth token present at ``cfg.auth_path``
  - LLM weights present (and non-empty) at ``cfg.models_dir / cfg.llm.filename``
  - macOS only: whether the user has completed the in-app permissions
    onboarding (marker file at ``cfg.data_dir/.permissions_onboarded_v1``)
  - macOS only (opt-in): Audio Capture permission by briefly spawning the
    ``audio-tap`` helper. DO NOT enable this at service startup — spawning
    audio-tap triggers the OS "Audio Capture" TCC dialog, and we want that
    dialog to appear only after the GUI has shown the user an explanation.
    The probe is still useful from the GUI itself (``recheck_mac_permission``
    bridge call) when the user has been told what's about to happen.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from sayzo_agent.auth.store import TokenStore
from sayzo_agent.config import Config

log = logging.getLogger(__name__)

# Mirrors _EXIT_PERMISSION_DENIED in sayzo_agent/capture/system_mac.py.
_MAC_EXIT_PERMISSION_DENIED = 77

# Upper bound on the audio-tap probe. The real capture uses 2.0s; we stay
# a touch shorter here so detection doesn't noticeably delay service startup.
_MAC_PROBE_TIMEOUT_SECS = 1.5

# Marker file written by the Permissions onboarding screen after the user
# has been walked through mic / audio-capture / notifications. Versioned so
# we can force a re-onboard later by bumping the suffix if the screen
# materially changes (e.g. adds a new permission).
_PERMISSIONS_MARKER_NAME = ".permissions_onboarded_v1"


@dataclass
class SetupStatus:
    """Result of :func:`detect_setup`. See module docstring for semantics.

    ``has_mic_permission`` is tri-state on darwin:
      - ``True``  — audio-tap launched and survived the probe window
      - ``False`` — audio-tap exited with code 77 (explicit deny)
      - ``None``  — probe inconclusive or skipped (the default at service
                    startup, since probing would fire the TCC dialog before
                    the user has seen the in-app explanation)

    ``has_permissions_onboarded`` is macOS-only state. On non-darwin it is
    always ``True`` (the Permissions screen is macOS-specific). On darwin
    it is ``True`` once the user has clicked Continue on the Permissions
    screen (marker file written).

    ``is_complete`` is computed once at detection time rather than derived
    lazily, so the snapshot is stable across platform patches in tests and
    across platform-detection branches elsewhere.
    """

    has_token: bool
    has_model: bool
    has_mic_permission: bool | None
    has_permissions_onboarded: bool
    is_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_token": self.has_token,
            "has_model": self.has_model,
            "has_mic_permission": self.has_mic_permission,
            "has_permissions_onboarded": self.has_permissions_onboarded,
            "is_complete": self.is_complete,
        }


def _compute_is_complete(
    *,
    has_token: bool,
    has_model: bool,
    has_mic_permission: bool | None,
    has_permissions_onboarded: bool,
) -> bool:
    if not has_token or not has_model:
        return False
    if sys.platform == "darwin":
        # Onboarding is the gate we actually enforce. The probed mic
        # permission only blocks if it was explicitly False (recovery path
        # when the user previously denied); inconclusive (None) never blocks.
        if not has_permissions_onboarded:
            return False
        if has_mic_permission is False:
            return False
    return True


def _check_token(cfg: Config) -> bool:
    try:
        return TokenStore(cfg.auth_path).has_tokens()
    except Exception:
        log.warning("token probe failed", exc_info=True)
        return False


def _check_model(cfg: Config) -> bool:
    path = cfg.models_dir / cfg.llm.filename
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _check_permissions_onboarded(cfg: Config) -> bool:
    try:
        return (cfg.data_dir / _PERMISSIONS_MARKER_NAME).exists()
    except OSError:
        return False


def _check_mac_mic_permission() -> bool | None:
    if sys.platform != "darwin":
        return None
    try:
        # Imported lazily because the module imports asyncio + audio libs we
        # don't want to pay for on non-darwin detect calls.
        from sayzo_agent.capture.system_mac import _find_audio_tap

        binary = _find_audio_tap()
    except (FileNotFoundError, ImportError) as e:
        log.warning("audio-tap not found for permission probe: %s", e)
        return None

    try:
        result = subprocess.run(
            [binary],
            capture_output=True,
            timeout=_MAC_PROBE_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        # Still running past the timeout → it passed the permission gate.
        return True
    except OSError as e:
        log.warning("audio-tap probe failed to start: %s", e)
        return None

    if result.returncode == _MAC_EXIT_PERMISSION_DENIED:
        return False
    if result.returncode == 0:
        return True

    log.warning(
        "audio-tap probe exited with code %d; treating as inconclusive",
        result.returncode,
    )
    return None


def detect_setup(cfg: Config, *, probe_mac_permission: bool = False) -> SetupStatus:
    """Return a :class:`SetupStatus` snapshot for ``cfg``.

    ``probe_mac_permission`` defaults to ``False`` — the audio-tap spawn
    fires the TCC "Audio Capture" dialog, which the user must not see
    before the in-app explanation on the Permissions screen. Set it to
    ``True`` only from user-initiated contexts (e.g. the bridge's
    ``recheck_mac_permission`` call after the user has clicked through).
    """
    has_token = _check_token(cfg)
    has_model = _check_model(cfg)
    if sys.platform == "darwin" and probe_mac_permission:
        has_mic_permission: bool | None = _check_mac_mic_permission()
    else:
        has_mic_permission = None
    has_permissions_onboarded = (
        sys.platform != "darwin" or _check_permissions_onboarded(cfg)
    )
    is_complete = _compute_is_complete(
        has_token=has_token,
        has_model=has_model,
        has_mic_permission=has_mic_permission,
        has_permissions_onboarded=has_permissions_onboarded,
    )
    return SetupStatus(
        has_token=has_token,
        has_model=has_model,
        has_mic_permission=has_mic_permission,
        has_permissions_onboarded=has_permissions_onboarded,
        is_complete=is_complete,
    )
