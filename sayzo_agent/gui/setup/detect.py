"""Pure first-run setup detection.

No UI. No mutation. Called at service startup to decide whether to open the
first-run GUI window before launching the tray + capture pipeline.

The detection probes three signals:
  - auth token present at ``cfg.auth_path``
  - LLM weights present (and non-empty) at ``cfg.models_dir / cfg.llm.filename``
  - macOS only: Audio Capture permission granted (probed by briefly spawning
    the ``audio-tap`` helper binary and reading its exit code; the binary
    returns _EXIT_PERMISSION_DENIED=77 when the OS blocks it)
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


@dataclass
class SetupStatus:
    """Result of :func:`detect_setup`. See module docstring for semantics.

    ``has_mic_permission`` is tri-state on darwin:
      - ``True``  — audio-tap launched and survived the probe window
      - ``False`` — audio-tap exited with code 77 (explicit deny)
      - ``None``  — probe inconclusive (binary missing, unknown exit code, etc.)
                    or the platform isn't darwin. Treat as "don't block".

    ``is_complete`` is computed once at detection time rather than derived
    lazily, so the snapshot is stable across platform patches in tests and
    across platform-detection branches elsewhere.
    """

    has_token: bool
    has_model: bool
    has_mic_permission: bool | None
    is_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_token": self.has_token,
            "has_model": self.has_model,
            "has_mic_permission": self.has_mic_permission,
            "is_complete": self.is_complete,
        }


def _compute_is_complete(
    *, has_token: bool, has_model: bool, has_mic_permission: bool | None
) -> bool:
    if not has_token or not has_model:
        return False
    # Only explicit False blocks — None (unknown) lets the user proceed so
    # that a broken probe doesn't wedge the service in the setup window.
    # If permission truly is missing, the runtime PermissionError from the
    # capture stage at app.py will surface it later.
    if sys.platform == "darwin" and has_mic_permission is False:
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


def detect_setup(cfg: Config, *, probe_mac_permission: bool = True) -> SetupStatus:
    """Return a :class:`SetupStatus` snapshot for ``cfg``.

    ``probe_mac_permission=False`` skips the audio-tap spawn entirely; used
    by tests and by the GUI's ``recheck_mac_permission`` bridge call when it
    wants a cheap re-read of the other two signals only.
    """
    has_token = _check_token(cfg)
    has_model = _check_model(cfg)
    if sys.platform == "darwin" and probe_mac_permission:
        has_mic_permission: bool | None = _check_mac_mic_permission()
    else:
        has_mic_permission = None
    is_complete = _compute_is_complete(
        has_token=has_token,
        has_model=has_model,
        has_mic_permission=has_mic_permission,
    )
    return SetupStatus(
        has_token=has_token,
        has_model=has_model,
        has_mic_permission=has_mic_permission,
        is_complete=is_complete,
    )
