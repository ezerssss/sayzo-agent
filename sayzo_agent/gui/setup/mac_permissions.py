"""macOS-specific first-run permission helpers.

Each ``prompt_*`` function is user-initiated: it fires the underlying OS
API on demand so the TCC dialog appears *after* the in-app explanation,
not during service startup. All functions return ``bool | None`` (True=
granted, False=denied, None=inconclusive) and never raise — failures are
logged and flattened to ``None`` so the GUI can surface a neutral message
instead of crashing the bridge.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Mirrors _EXIT_PERMISSION_DENIED in sayzo_agent/capture/system_mac.py.
_MAC_EXIT_PERMISSION_DENIED = 77
# Matches the real capture's startup budget — long enough for the tap to
# pass the permission gate, short enough that a UI click doesn't stall.
_AUDIO_TAP_PROBE_TIMEOUT_SECS = 1.5
# Give the mic stream a beat to actually open before tearing it down. On
# macOS this is also when TCC blocks waiting on the user.
_MIC_OPEN_SETTLE_SECS = 0.1

# x-apple.systempreferences URIs for the three Privacy & Security sub-panes
# we care about. There's no public Audio Capture sub-pane URI, so the tap
# deep-link still lands on the general Privacy & Security screen on modern
# macOS — the user scrolls to find Sayzo Agent under "Audio Capture".
_MIC_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)
_AUDIO_CAPTURE_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture"
)
_NOTIFICATIONS_DEEPLINK = (
    "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
)

# Lazy DesktopNotifierSync singleton. Repeated construction is expensive and
# pointless — the backend binding is stateless across calls once built.
_NOTIFIER = None
_NOTIFIER_INIT_FAILED = False
_NOTIFIER_LOCK = threading.Lock()


def _get_notifier():
    global _NOTIFIER, _NOTIFIER_INIT_FAILED
    if _NOTIFIER is not None or _NOTIFIER_INIT_FAILED:
        return _NOTIFIER
    with _NOTIFIER_LOCK:
        if _NOTIFIER is not None or _NOTIFIER_INIT_FAILED:
            return _NOTIFIER
        try:
            from desktop_notifier.sync import DesktopNotifierSync

            _NOTIFIER = DesktopNotifierSync(app_name="Sayzo.Agent")
        except Exception:
            _NOTIFIER_INIT_FAILED = True
            log.warning(
                "[mac_permissions] DesktopNotifierSync init failed", exc_info=True
            )
    return _NOTIFIER


def prompt_microphone() -> Optional[bool]:
    """Briefly open a sounddevice InputStream to trigger the Microphone TCC
    dialog on first call. On subsequent calls the OS silently honors the
    previously-recorded decision (no dialog re-appears).

    Returns True on success, False on PortAudioError (explicit deny or the
    device is otherwise unavailable), None on unexpected failure.
    """
    if sys.platform != "darwin":
        return None
    try:
        import sounddevice as sd
    except Exception:
        log.warning("[mac_permissions] sounddevice import failed", exc_info=True)
        return None

    stream = None
    try:
        stream = sd.InputStream(
            samplerate=16000, channels=1, blocksize=160, dtype="float32"
        )
        stream.start()
        # Let the stream fully open so TCC (if it's going to block) has time
        # to actually surface the dialog before we tear the stream down.
        time.sleep(_MIC_OPEN_SETTLE_SECS)
        return True
    except sd.PortAudioError:
        log.warning(
            "[mac_permissions] mic stream open failed (likely denied)",
            exc_info=True,
        )
        return False
    except Exception:
        log.warning(
            "[mac_permissions] mic probe unexpected failure", exc_info=True
        )
        return None
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


def prompt_audio_capture() -> Optional[bool]:
    """Spawn the audio-tap Swift binary briefly. First call triggers the
    Audio Capture TCC dialog (via AudioHardwareCreateProcessTap).

    Returns:
      - True  → probe survived the timeout window (permission granted; the
                tap would keep running if we didn't kill it)
      - False → exit 77 (explicit deny from the Swift binary)
      - None  → binary missing, spawn failed, or unknown exit code
    """
    if sys.platform != "darwin":
        return None
    try:
        from sayzo_agent.capture.system_mac import _find_audio_tap

        binary = _find_audio_tap()
    except (FileNotFoundError, ImportError) as e:
        log.warning("[mac_permissions] audio-tap not found: %s", e)
        return None

    try:
        result = subprocess.run(
            [binary],
            capture_output=True,
            timeout=_AUDIO_TAP_PROBE_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return True
    except OSError as e:
        log.warning("[mac_permissions] audio-tap spawn failed: %s", e)
        return None

    if result.returncode == _MAC_EXIT_PERMISSION_DENIED:
        return False
    if result.returncode == 0:
        return True
    log.warning(
        "[mac_permissions] audio-tap exited with code %d (inconclusive)",
        result.returncode,
    )
    return None


def prompt_notifications() -> Optional[bool]:
    """Call ``DesktopNotifierSync.request_authorisation`` — first call on
    macOS triggers the UNUserNotificationCenter dialog. Returns True on
    grant, False on deny, None on error."""
    if sys.platform != "darwin":
        return None
    notifier = _get_notifier()
    if notifier is None:
        return None
    try:
        return bool(notifier.request_authorisation())
    except Exception:
        log.warning(
            "[mac_permissions] request_authorisation failed", exc_info=True
        )
        return None


def _open(deeplink: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(["open", deeplink])
    except OSError as e:
        log.warning("[mac_permissions] open '%s' failed: %s", deeplink, e)


def open_mic_settings() -> None:
    _open(_MIC_DEEPLINK)


def open_audio_capture_settings() -> None:
    _open(_AUDIO_CAPTURE_DEEPLINK)


def open_notification_settings() -> None:
    _open(_NOTIFICATIONS_DEEPLINK)
