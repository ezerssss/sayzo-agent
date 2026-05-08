"""macOS-specific first-run permission helpers.

Each ``prompt_*`` function is user-initiated: it fires the underlying OS
API on demand so the TCC dialog appears *after* the in-app explanation,
not during service startup. Most return ``bool | None`` (True=granted,
False=denied, None=inconclusive) and never raise — failures are logged
and flattened to ``None`` so the GUI can surface a neutral message
instead of crashing the bridge. ``prompt_microphone`` and
``prompt_audio_capture`` return a richer :class:`TccPromptResult` so the
GUI can distinguish a true denial from a stale-TCC silent-deny.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import NamedTuple, Optional

log = logging.getLogger(__name__)

# Mirrors _EXIT_PERMISSION_DENIED in sayzo_agent/capture/system_mac.py.
_MAC_EXIT_PERMISSION_DENIED = 77

# Upper bound on how long we'll block the bridge waiting for the user to
# answer a TCC dialog. The dialog is system-modal (foreground), so 2 minutes
# is generous for read-and-click; anything longer almost certainly means the
# dialog never appeared (signing / bundle config issue) and we should
# surface "inconclusive" so the GUI can route to the Open-Settings escape.
_TCC_REQUEST_TIMEOUT_SECS = 120.0

# audio-tap stderr signature emitted from main.swift after
# AudioHardwareCreateProcessTap and AudioDeviceStart succeed (= TCC granted).
# If we see this line, the binary is past the TCC gate and capturing.
_AUDIO_TAP_SUCCESS_NEEDLE = "capturing system audio"

# Substring of audio-tap stderr emitted just before exit(77) on TCC denial.
# Used as a secondary signal in case exit code is observed via an unusual
# path (e.g. test harness intercepting wait()).
_AUDIO_TAP_DENIED_NEEDLE = "AudioHardwareCreateProcessTap failed"

# Hard kill grace after we send SIGTERM during teardown.
_PROBE_TERMINATE_GRACE_SECS = 2.0

# AVAuthorizationStatus enum (AVFoundation/AVCaptureDevice.h). Stable across
# macOS releases.
_AV_AUTH_NOT_DETERMINED = 0
_AV_AUTH_RESTRICTED = 1
_AV_AUTH_DENIED = 2
_AV_AUTH_AUTHORIZED = 3

# A "denied" answer that arrives faster than this is almost certainly a
# silent-deny pattern: either a stale TCC entry whose code-requirement
# doesn't match the current bundle (the system fires the completion
# handler with False immediately without presenting UI), or — on a Sayzo-
# specific note — a missing `NSMicrophoneUsageDescription` /
# `NSAudioCaptureUsageDescription` key in the bundle's Info.plist (which
# AVFoundation rejects at the pre-flight check, before any TCC dialog).
# A real human read-and-click takes much longer than 500 ms, even on a
# snap decision.
_STALE_TCC_THRESHOLD_SECS = 0.5

# Bundle identifier our TCC entries are keyed under. Mirrors the
# `bundle_identifier` in sayzo-agent.spec's BUNDLE() call. Hard-coded
# (not read from sys.executable's bundle) because we want `tccutil reset`
# and Info.plist diagnostics to operate on the value we INTEND, not what
# the running bundle happens to advertise — if the bundle ID ever drifts
# in a build, we want the discrepancy to be loud, not silent.
_BUNDLE_ID = "com.sayzo.agent"

# `tccutil` service identifiers. "Microphone" matches the documented
# service in `man tccutil`. "AudioCapture" is the private TCC service
# string `kTCCServiceAudioCapture` used by `AudioHardwareCreateProcessTap`
# (macOS 14.4+). Apple's `man tccutil` does not document AudioCapture
# explicitly — service name sourced from the canonical `insidegui/AudioCap`
# sample which calls the private `TCCAccessRequest` API directly. See
# https://github.com/insidegui/AudioCap.
_TCC_SERVICE_MICROPHONE = "Microphone"
_TCC_SERVICE_AUDIO_CAPTURE = "AudioCapture"

# Subprocess timeout for `tccutil` and similar helpers. tccutil is fast —
# 5 s is comfortably above any real-world execution time and below any
# UI patience threshold.
_SUBPROCESS_TIMEOUT_SECS = 5.0


class TccPromptResult(NamedTuple):
    """Outcome of :func:`prompt_microphone` and :func:`prompt_audio_capture`.

    ``granted`` is the bool/None tri-state every other ``prompt_*`` helper
    returns. ``stale_tcc_likely`` is True when the heuristic fingerprints a
    stale TCC entry — the GUI uses that flag to swap the generic "blocked"
    message for the targeted "remove from System Settings, then retry"
    recovery steps. False on every other outcome (including legitimate
    denials, timeouts, and granted).

    Both Microphone (AVFoundation) and Audio Capture (CoreAudio Process
    Taps via the Swift audio-tap helper) can hit the same root cause: a
    TCC entry from a pre-v2.6.0 ad-hoc-signed Sayzo install whose code
    requirement no longer matches the current Developer-ID-signed bundle.
    """

    granted: Optional[bool]
    stale_tcc_likely: bool


# Backward-compat alias — earlier this module exposed the type as
# MicPromptResult before the audio-capture path adopted the same shape.
MicPromptResult = TccPromptResult

# x-apple.systempreferences URIs for the three Privacy & Security sub-panes
# we care about. There's no public Audio Capture sub-pane URI, so the tap
# deep-link still lands on the general Privacy & Security screen on modern
# macOS — the user scrolls to find Sayzo under "Audio Capture".
_MIC_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)
_AUDIO_CAPTURE_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture"
)
_NOTIFICATIONS_DEEPLINK = (
    "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
)
_ACCESSIBILITY_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
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
            from desktop_notifier import Icon
            from desktop_notifier.sync import DesktopNotifierSync

            from sayzo_agent.gui.common.assets import notification_icon_path

            # Bump desktop-notifier's own logger so its codesign /
            # auth-grant lines flow into agent.log alongside our own
            # ``[mac_permissions]`` lines. Mirrors what notify.py does
            # for the runtime notifier.
            try:
                logging.getLogger("desktop_notifier").setLevel(logging.INFO)
            except Exception:
                pass

            # Pass our own logo so the test toast shows the Sayzo icon
            # instead of desktop-notifier's bundled python.png. Same fix
            # we apply on Windows; on macOS UNN it shows up next to the
            # title in the Notification Center.
            icon_p = notification_icon_path()
            app_icon = Icon(path=icon_p) if icon_p else None
            log.info(
                "[mac_permissions] notifier init: icon=%s exists=%s",
                icon_p,
                icon_p.exists() if icon_p else False,
            )
            _NOTIFIER = DesktopNotifierSync(app_name="Sayzo", app_icon=app_icon)
            log.info(
                "[mac_permissions] DesktopNotifierSync init OK"
            )
            # Bundle introspection — mirrors notify.py so users running
            # diagnose-notifications and users hitting the onboarding
            # test-toast path get the same identity log either way.
            try:
                from desktop_notifier.backends.macos_support import (  # type: ignore[import-not-found]
                    is_bundle,
                    is_signed_bundle,
                )

                log.info(
                    "[mac_permissions] bundle: is_bundle=%s is_signed=%s",
                    is_bundle(),
                    is_signed_bundle(),
                )
            except Exception:
                log.debug(
                    "[mac_permissions] bundle introspection failed",
                    exc_info=True,
                )
        except Exception:
            _NOTIFIER_INIT_FAILED = True
            log.warning(
                "[mac_permissions] DesktopNotifierSync init failed", exc_info=True
            )
    return _NOTIFIER


def _log_bundle_info_plist_once() -> None:
    """One-shot diagnostic: log the bundle's actual Info.plist values so we
    can tell — from a user's agent.log — whether the TCC failure is a
    bundle/build problem (usage-description key missing) or a stale-TCC
    problem (key present, request still silently denied).

    Apple's documented behavior: when a bundle calls
    ``AVCaptureDevice.requestAccessForMediaType:`` for an audio media type
    without ``NSMicrophoneUsageDescription`` in its Info.plist, the system
    rejects the request at the pre-flight check and fires the completion
    handler with False immediately, without showing a dialog. That symptom
    is indistinguishable at runtime from a stale-CR silent-deny — except
    that the missing-key case will (a) never let the bundle appear in
    System Settings → Privacy & Security → Microphone (no entry can be
    created), and (b) be visible in this log line. See
    https://developer.apple.com/documentation/BundleResources/Information-Property-List/NSMicrophoneUsageDescription
    """
    if sys.platform != "darwin":
        return
    if getattr(_log_bundle_info_plist_once, "_done", False):
        return
    _log_bundle_info_plist_once._done = True  # type: ignore[attr-defined]

    try:
        from Foundation import NSBundle  # type: ignore[import-not-found]
    except Exception:
        log.warning(
            "[mac_permissions] Foundation.NSBundle import failed — "
            "cannot diagnose bundle Info.plist",
            exc_info=True,
        )
        return

    try:
        main_bundle = NSBundle.mainBundle()
        info = main_bundle.infoDictionary() or {}
        bundle_path = main_bundle.bundlePath()
    except Exception:
        log.warning(
            "[mac_permissions] NSBundle.mainBundle().infoDictionary() raised",
            exc_info=True,
        )
        return

    # Truthy presence + first 60 chars of each usage description so a user
    # uploading agent.log to us doesn't accidentally leak surrounding text,
    # but we can confirm the key actually has a non-empty string value
    # (PyInstaller writes our spec dict via plistlib — a typo or empty
    # value would surface here).
    def _summarize(key: str) -> str:
        val = info.get(key)
        if val is None:
            return "MISSING"
        s = str(val)
        return f"present ({len(s)} chars: {s[:60]!r})"

    log.info(
        "[mac_permissions] bundle Info.plist diagnostic: "
        "path=%s bundle_id=%r executable=%r LSUIElement=%r",
        bundle_path,
        info.get("CFBundleIdentifier"),
        info.get("CFBundleExecutable"),
        info.get("LSUIElement"),
    )
    log.info(
        "[mac_permissions] usage descriptions: "
        "NSMicrophoneUsageDescription=%s NSAudioCaptureUsageDescription=%s "
        "NSAppleEventsUsageDescription=%s",
        _summarize("NSMicrophoneUsageDescription"),
        _summarize("NSAudioCaptureUsageDescription"),
        _summarize("NSAppleEventsUsageDescription"),
    )


def _tccutil_reset_service(service: str) -> bool:
    """Run ``tccutil reset <service> com.sayzo.agent`` for the current
    user's TCC database.

    Returns True on rc=0, False otherwise. Best-effort — never raises.

    ``tccutil`` does NOT require sudo for the current user's TCC database;
    it can clear any entry for our own bundle. The reset is idempotent:
    if no entry exists for the bundle/service, ``tccutil`` exits 0 anyway.
    Apple's documented behavior is that an already-running process must
    be relaunched after a reset before subsequent ``requestAccess`` calls
    will surface a fresh dialog — AVFoundation caches the
    NotDetermined→Denied transition per process. Callers should pair this
    with a relaunch.

    See https://developer.apple.com/forums/thread/679303 and
    https://discussions.apple.com/thread/254893066.
    """
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["tccutil", "reset", service, _BUNDLE_ID],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            text=True,
        )
    except Exception:
        log.warning(
            "[mac_permissions] tccutil reset %s raised", service, exc_info=True
        )
        return False
    if result.returncode == 0:
        log.info(
            "[mac_permissions] tccutil reset %s %s ok (stdout=%r)",
            service,
            _BUNDLE_ID,
            result.stdout.strip(),
        )
        return True
    log.warning(
        "[mac_permissions] tccutil reset %s %s failed: rc=%d stderr=%r",
        service,
        _BUNDLE_ID,
        result.returncode,
        result.stderr.strip(),
    )
    return False


def _collect_bundle_info() -> dict:
    """Read the running bundle's Info.plist via NSBundle.mainBundle.

    Shared by :func:`_log_bundle_info_plist_once` (one-shot startup
    diagnostic) and :func:`gather_tcc_diagnostic_text` (user-triggered
    Copy Diagnostic Info button). Returns an empty dict on non-darwin
    or any introspection failure — callers fall back to placeholder
    output rather than raising into the UI.
    """
    if sys.platform != "darwin":
        return {}
    try:
        from Foundation import NSBundle  # type: ignore[import-not-found]
        main_bundle = NSBundle.mainBundle()
        info = main_bundle.infoDictionary() or {}
        return {
            "bundle_path": str(main_bundle.bundlePath()),
            "bundle_id": str(info.get("CFBundleIdentifier") or ""),
            "executable": str(info.get("CFBundleExecutable") or ""),
            "ls_ui_element": bool(info.get("LSUIElement") or False),
            "NSMicrophoneUsageDescription": str(
                info.get("NSMicrophoneUsageDescription") or ""
            ),
            "NSAudioCaptureUsageDescription": str(
                info.get("NSAudioCaptureUsageDescription") or ""
            ),
            "NSAppleEventsUsageDescription": str(
                info.get("NSAppleEventsUsageDescription") or ""
            ),
        }
    except Exception:
        log.debug("[mac_permissions] _collect_bundle_info raised", exc_info=True)
        return {}


def gather_tcc_diagnostic_text(cfg) -> str:
    """Build a plain-text diagnostic summary the user can paste into a
    support thread when "Reset & Restart Sayzo" hasn't fixed the silent-
    deny.

    Sections:
        - Sayzo version + macOS version
        - Bundle path, identifier, executable, LSUIElement
        - Presence + length of NSMicrophoneUsageDescription /
          NSAudioCaptureUsageDescription / NSAppleEventsUsageDescription
          (the value itself is NOT included — just length — so users don't
          accidentally paste irrelevant copy back to us)
        - ``codesign -dvv`` output for the bundle (designated requirement,
          authority, team identifier — tells us whether a CR mismatch
          really is the cause)
        - Last 50 lines of ``agent.log`` filtered to ``[mac_permissions]``
          / ``[mac_heal]`` markers (the TCC story)

    Plain text intentionally — Slack and email render it cleanly, no
    Markdown surprises. Returns a string ready for clipboard paste; the
    function never raises.
    """
    import datetime as _dt
    import platform as _platform

    lines: list[str] = []
    # `utcnow()` is deprecated for removal — use the timezone-aware
    # equivalent so the report's leading line keeps working on future
    # Pythons without warnings.
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"Sayzo TCC diagnostic — {now}")

    try:
        from importlib.metadata import version as _pkg_version

        lines.append(f"Sayzo version: {_pkg_version('sayzo-agent')}")
    except Exception:
        lines.append("Sayzo version: unknown")

    lines.append(f"Platform: {sys.platform}")
    if sys.platform == "darwin":
        try:
            mv = _platform.mac_ver()
            lines.append(f"macOS version: {mv[0]} ({mv[2]})")
        except Exception:
            lines.append("macOS version: unknown")

    info = _collect_bundle_info()
    lines.append("")
    if info:
        lines.append(f"Bundle path: {info.get('bundle_path')}")
        lines.append(f"  CFBundleIdentifier: {info.get('bundle_id')!r}")
        lines.append(f"  CFBundleExecutable: {info.get('executable')!r}")
        lines.append(f"  LSUIElement: {info.get('ls_ui_element')}")
        for key in (
            "NSMicrophoneUsageDescription",
            "NSAudioCaptureUsageDescription",
            "NSAppleEventsUsageDescription",
        ):
            v = info.get(key) or ""
            if v:
                lines.append(f"  {key}: present ({len(v)} chars)")
            else:
                # MISSING is the smoking gun for AVFoundation pre-flight
                # silent-deny — flag it loudly.
                lines.append(f"  {key}: *** MISSING ***")
    else:
        lines.append("Bundle: <unable to introspect via NSBundle>")

    # codesign output goes to stderr by convention (a long-standing Apple
    # quirk). We merge stdout + stderr so the user gets one block to
    # paste regardless of which stream the lines arrive on.
    bundle_path = info.get("bundle_path")
    lines.append("")
    if sys.platform == "darwin" and bundle_path:
        lines.append("codesign -dvv:")
        try:
            cs = subprocess.run(
                ["codesign", "-dvv", "--", bundle_path],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
            )
            cs_text = (cs.stdout + cs.stderr).strip()
            for line in cs_text.splitlines():
                lines.append(f"  {line}")
            if cs.returncode != 0:
                lines.append(f"  (rc={cs.returncode})")
        except Exception as e:
            lines.append(f"  (codesign call failed: {e!r})")
    else:
        lines.append("codesign: skipped (non-darwin or no bundle path)")

    log_path = cfg.logs_dir / "agent.log"
    lines.append("")
    lines.append(f"Last 50 [mac_permissions]/[mac_heal] log lines from {log_path}:")
    try:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            wanted = [
                ln for ln in text.splitlines()
                if "[mac_permissions]" in ln or "[mac_heal]" in ln
            ]
            if not wanted:
                lines.append("  (no [mac_permissions]/[mac_heal] lines yet)")
            else:
                for ln in wanted[-50:]:
                    lines.append(f"  {ln}")
        else:
            lines.append(f"  (log file not present at {log_path})")
    except Exception as e:
        lines.append(f"  (read failed: {e!r})")

    return "\n".join(lines) + "\n"


def copy_diagnostic_to_clipboard(cfg) -> bool:
    """Pipe the TCC diagnostic text into ``pbcopy`` so the user can
    paste it into a support thread with one keystroke.

    Returns True on rc=0, False otherwise. macOS-only (Windows uses
    ``clip``, but the recovery UI surfaces this button only on the
    macOS stale-TCC path so we keep the helper darwin-scoped).
    """
    if sys.platform != "darwin":
        return False
    text = gather_tcc_diagnostic_text(cfg)
    try:
        proc = subprocess.run(
            ["pbcopy"],
            input=text,
            text=True,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
        )
    except Exception:
        log.warning(
            "[mac_permissions] pbcopy raised", exc_info=True
        )
        return False
    if proc.returncode == 0:
        log.info(
            "[mac_permissions] copied %d-char TCC diagnostic to clipboard",
            len(text),
        )
        return True
    log.warning(
        "[mac_permissions] pbcopy failed: rc=%d stderr=%r",
        proc.returncode,
        proc.stderr.strip(),
    )
    return False


def relaunch_app() -> None:
    """Relaunch the Sayzo .app bundle and hard-exit this process.

    Used as the second half of a "Reset Permission" recovery flow:
    ``tccutil reset`` clears the orphan TCC entry, then the running
    process must die so AVFoundation re-reads from a fresh database on
    the next launch (Apple caches the NotDetermined→Denied transition
    per process; see `_tccutil_reset_service` docstring for the source).

    Safe to call from any code path: hard-exits unconditionally on macOS,
    no-op on other platforms. The relaunch uses ``open -n`` (new
    instance, detached session) so the new process is fully independent
    of this one — kernel-level pidfile locking in
    :mod:`sayzo_agent.pidfile` ensures the new instance only proceeds
    once we're gone.
    """
    if sys.platform != "darwin":
        return
    try:
        from pathlib import Path

        exe = Path(sys.executable).resolve()
        app_bundle = next(
            (p for p in exe.parents if p.suffix == ".app"), None
        )
        if app_bundle is None or not app_bundle.exists():
            log.warning(
                "[mac_permissions] relaunch_app: no .app bundle above %s — "
                "exiting without relaunch",
                exe,
            )
        else:
            subprocess.Popen(
                ["open", "-n", str(app_bundle)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.warning(
                "[mac_permissions] relaunch_app: relaunching %s", app_bundle
            )
    except Exception:
        log.warning(
            "[mac_permissions] relaunch_app: spawn failed",
            exc_info=True,
        )
    import os
    os._exit(0)


def prompt_microphone() -> TccPromptResult:
    """Read or trigger the macOS Microphone TCC decision and return the
    actual outcome plus a stale-TCC hint.

    Uses ``AVCaptureDevice``'s public TCC API:

    - ``authorizationStatusForMediaType:`` is a sync read of the recorded
      TCC state — never prompts. If the user has already decided we
      short-circuit and return without firing a dialog.
    - ``requestAccessForMediaType:completionHandler:`` only fires the
      dialog when the recorded status is NotDetermined. The completion
      handler runs on a background queue when the user clicks; we block
      this thread on a ``threading.Event`` so the bridge call only returns
      *after* the user has actually decided.

    Replaces the old "open sounddevice, sleep 0.1s, return True" path,
    which cheerfully returned ``granted=True`` while the dialog was still
    on screen — and silently returned ``True`` on a denied bundle because
    sounddevice opens a "permission denied" stream and reads zeros instead
    of raising. That bug is what made the v2.6.0 macOS hotkey path record
    audio_dur > 0 with mic_total = 0.

    The ``stale_tcc_likely`` field of the result is True when the request
    branch returned False in under
    :data:`_STALE_TCC_THRESHOLD_SECS` — that's the fingerprint of a TCC
    entry from a previous Sayzo install with a different signing identity
    silently denying the request without presenting the dialog. The GUI
    surfaces a targeted recovery message for this case.

    Returns a :class:`TccPromptResult`:
        granted=True   — authorized
        granted=False  — denied / restricted / declined in the dialog
        granted=None   — AVFoundation unavailable or dialog timeout
        stale_tcc_likely=True only when the silent-deny pattern fires.
    """
    if sys.platform != "darwin":
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    # First call into prompt_microphone in this process is also the most
    # useful place to dump the bundle's actual Info.plist values. If
    # `NSMicrophoneUsageDescription` is missing, the request below will
    # silent-deny in milliseconds — the diagnostic line tells us
    # (and the user, on a support thread) which root cause we're
    # looking at instead of having to guess from the False alone.
    _log_bundle_info_plist_once()

    try:
        # AVFoundation framework binding from pyobjc-framework-AVFoundation.
        # Bundled on the build host; on a dev machine without the binding,
        # we fall back to None so the GUI can still show the Open-Settings
        # path instead of crashing the bridge.
        from AVFoundation import (  # type: ignore[import-not-found]
            AVCaptureDevice,
            AVMediaTypeAudio,
        )
    except Exception:
        log.warning(
            "[mac_permissions] AVFoundation import failed — cannot read mic TCC",
            exc_info=True,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    try:
        status = AVCaptureDevice.authorizationStatusForMediaType_(
            AVMediaTypeAudio
        )
    except Exception:
        log.warning(
            "[mac_permissions] authorizationStatusForMediaType_ raised",
            exc_info=True,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    log.info(
        "[mac_permissions] microphone TCC: status=%r media_type=%r thread=%s",
        status,
        AVMediaTypeAudio,
        threading.current_thread().name,
    )

    if status == _AV_AUTH_AUTHORIZED:
        log.info("[mac_permissions] microphone TCC: already authorized")
        return TccPromptResult(granted=True, stale_tcc_likely=False)
    if status == _AV_AUTH_DENIED:
        log.info("[mac_permissions] microphone TCC: previously denied")
        return TccPromptResult(granted=False, stale_tcc_likely=False)
    if status == _AV_AUTH_RESTRICTED:
        log.info("[mac_permissions] microphone TCC: restricted (MDM/parental)")
        return TccPromptResult(granted=False, stale_tcc_likely=False)
    if status != _AV_AUTH_NOT_DETERMINED:
        log.warning(
            "[mac_permissions] microphone TCC: unexpected status=%r", status
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    # NotDetermined → fire the dialog from the MAIN thread. AVFoundation's
    # requestAccessForMediaType_completionHandler_ doesn't reliably present
    # its dialog when invoked from a non-main thread inside a frozen
    # pywebview bundle — instead the framework silently no-ops the dialog
    # and fires the completion handler with `denied` in milliseconds. This
    # is exactly what we hit in v2.7.0: the "requesting" log was followed
    # by "user response → False" with a 6 ms gap on a user who had never
    # seen a dialog. Apple's docs say the call is thread-safe, but in
    # practice the dialog presentation needs to be scheduled onto the main
    # NSRunLoop. NSOperationQueue.mainQueue() is the cleanest cross-version
    # way to do that — pywebview's WebView keeps the main runloop pumping,
    # so the block runs as soon as the runloop is idle.
    try:
        from Foundation import NSOperationQueue  # type: ignore[import-not-found]
    except Exception:
        log.warning(
            "[mac_permissions] Foundation import failed — cannot dispatch TCC request",
            exc_info=True,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    log.info(
        "[mac_permissions] microphone TCC: requesting (dispatching to main queue)"
    )
    event = threading.Event()
    granted_holder: list[Optional[bool]] = [None]

    def completion(granted) -> None:
        # Always set the event, even if the bool coercion blows up. The
        # caller must not sit on Event.wait() forever.
        try:
            granted_holder[0] = bool(granted)
        finally:
            event.set()

    def fire_request() -> None:
        # Runs on the main thread via NSOperationQueue.mainQueue.
        try:
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVMediaTypeAudio, completion
            )
        except Exception:
            log.warning(
                "[mac_permissions] requestAccessForMediaType raised on main",
                exc_info=True,
            )
            event.set()  # unblock the caller — outcome stays None

    request_started = time.monotonic()
    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(fire_request)
    except Exception:
        log.warning(
            "[mac_permissions] addOperationWithBlock_ failed — cannot fire dialog",
            exc_info=True,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    if not event.wait(timeout=_TCC_REQUEST_TIMEOUT_SECS):
        log.warning(
            "[mac_permissions] microphone TCC: dialog timed out after %.0fs "
            "(no callback fired — likely no dialog actually presented)",
            _TCC_REQUEST_TIMEOUT_SECS,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    elapsed = time.monotonic() - request_started
    result = granted_holder[0]
    # Stale-TCC fingerprint: an entry created by a previous Sayzo install
    # with a different signing identity (e.g. v2.5.x ad-hoc → v2.6.0+
    # Developer-ID) silently denies the request without presenting UI.
    # The completion fires with False in milliseconds. A real human can't
    # read the dialog and click Don't Allow that fast, so a sub-500ms
    # False from the request branch is a high-confidence stale-TCC signal.
    stale_tcc_likely = (
        result is False and elapsed < _STALE_TCC_THRESHOLD_SECS
    )
    log.info(
        "[mac_permissions] microphone TCC: user response → %s (elapsed=%.3fs, stale_tcc_likely=%s)",
        result,
        elapsed,
        stale_tcc_likely,
    )
    return TccPromptResult(granted=result, stale_tcc_likely=stale_tcc_likely)


def prompt_audio_capture() -> TccPromptResult:
    """Spawn the audio-tap Swift binary and wait for its actual TCC
    decision before returning.

    On first launch ``AudioHardwareCreateProcessTap`` (inside the Swift
    binary) blocks on the system Audio Capture TCC dialog. Two outcomes
    we can observe from the calling Python process:

    - **Granted**: the binary proceeds past the API call, calls
      ``AudioDeviceStart``, and prints
      ``"audio-tap: capturing system audio …"`` to stderr. We see that
      line, send SIGTERM to the still-running probe, and report True.
    - **Denied**: the API returns non-zero, the binary prints a hint to
      stderr and ``exit(77)``. We observe the exit code, report False.

    The previous implementation — ``subprocess.run`` with a 1.5 s timeout,
    treating any timeout as success — was a placebo: the TCC dialog
    almost always takes longer than 1.5 s for a human to read and click,
    so we returned ``granted=True`` while the binary was still sitting on
    the blocker waiting for the user. That is exactly the bug the user
    flagged ("sometimes even if I haven't clicked yes it cheerfully
    updates the gui that it is accepted").

    Stale-TCC detection: a TCC entry from a pre-v2.6.0 (ad-hoc-signed)
    audio-tap binary won't match the current Developer-ID-signed binary's
    code requirement, so the OS silent-denies without presenting UI. The
    fingerprint is the same as the mic path: a False answer arriving
    faster than a human read+click. The GUI uses
    ``stale_tcc_likely=True`` to swap the generic "blocked" copy for the
    "remove from System Settings, then retry" recovery flow.

    Returns:
        granted=True   — binary printed the success line (TCC granted)
        granted=False  — binary exited with code 77 (TCC denied)
        granted=None   — binary missing, spawn failed, or dialog timed out
        stale_tcc_likely=True only when the silent-deny pattern fires.
    """
    if sys.platform != "darwin":
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    # Same diagnostic call as prompt_microphone — if
    # NSAudioCaptureUsageDescription is missing, audio-tap will exit 77
    # in milliseconds and the heuristic will flag stale_tcc_likely
    # incorrectly. The Info.plist log line tells us which root cause it is.
    _log_bundle_info_plist_once()

    try:
        from sayzo_agent.capture.system_mac import _find_audio_tap

        binary = _find_audio_tap()
    except (FileNotFoundError, ImportError) as e:
        log.warning("[mac_permissions] audio-tap not found: %s", e)
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    request_started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [binary],
            # We don't care about the PCM bytes for permission probing,
            # and not draining stdout would eventually block the binary
            # on a full pipe. DEVNULL discards them safely.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as e:
        log.warning("[mac_permissions] audio-tap spawn failed: %s", e)
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    log.info(
        "[mac_permissions] audio-tap TCC: probe spawned (pid=%d), waiting for user decision",
        proc.pid,
    )

    granted_event = threading.Event()
    granted_holder: list[Optional[bool]] = [None]
    stderr_tail: list[str] = []

    def reader() -> None:
        """Stream stderr until we see the success line or hit EOF."""
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                # Keep the last few lines for diagnostic logging on
                # inconclusive exits; cap the buffer so a chatty binary
                # can't grow it without bound.
                stderr_tail.append(line.rstrip())
                if len(stderr_tail) > 20:
                    del stderr_tail[0]
                if _AUDIO_TAP_SUCCESS_NEEDLE in line:
                    granted_holder[0] = True
                    granted_event.set()
                    return
                if _AUDIO_TAP_DENIED_NEEDLE in line:
                    # Likely about to exit 77 — flag it, but let the
                    # main thread confirm via exit code below.
                    granted_holder[0] = False
        finally:
            # Always signal the main thread, even on EOF without a
            # decisive line — the main thread will then read exit code.
            granted_event.set()

    reader_thread = threading.Thread(
        target=reader, daemon=True, name="audio-tap-stderr"
    )
    reader_thread.start()

    got_signal = granted_event.wait(timeout=_TCC_REQUEST_TIMEOUT_SECS)

    if not got_signal:
        log.warning(
            "[mac_permissions] audio-tap TCC: timed out after %.0fs — "
            "dialog likely never presented",
            _TCC_REQUEST_TIMEOUT_SECS,
        )
        _terminate(proc)
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    if granted_holder[0] is True:
        log.info(
            "[mac_permissions] audio-tap TCC: granted (saw success line on stderr)"
        )
        _terminate(proc)
        return TccPromptResult(granted=True, stale_tcc_likely=False)

    # Either reader saw the deny needle or stderr hit EOF. Either way the
    # binary is on its way out — wait briefly for the actual exit code.
    try:
        rc = proc.wait(timeout=_PROBE_TERMINATE_GRACE_SECS)
    except subprocess.TimeoutExpired:
        log.warning(
            "[mac_permissions] audio-tap TCC: stderr signaled but process still alive — terminating"
        )
        _terminate(proc)
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    if rc == _MAC_EXIT_PERMISSION_DENIED:
        elapsed = time.monotonic() - request_started
        # Same heuristic as prompt_microphone: a sub-500 ms denial means
        # no TCC dialog was presented — the OS silently denied because of
        # a CR mismatch with a stale entry from a previous Sayzo install.
        stale_tcc_likely = elapsed < _STALE_TCC_THRESHOLD_SECS
        log.info(
            "[mac_permissions] audio-tap TCC: denied (exit 77, elapsed=%.3fs, stale_tcc_likely=%s)",
            elapsed,
            stale_tcc_likely,
        )
        return TccPromptResult(
            granted=False, stale_tcc_likely=stale_tcc_likely
        )

    # Negative return codes mean the binary was killed by a signal
    # (subprocess returncode == -signum). On MDM-managed Macs the common
    # one is `-6` = SIGABRT, which typically means Gatekeeper / library-
    # validation aborted an unsigned-and-quarantined helper before it
    # could execute. Surface stderr tail so agent.log captures whatever
    # the binary printed before dying — the difference between "PPPC
    # denied" and "Gatekeeper killed it" is product fix vs packaging fix.
    log.warning(
        "[mac_permissions] audio-tap TCC: inconclusive exit (rc=%d); stderr_tail=%r",
        rc,
        stderr_tail[-5:],
    )
    return TccPromptResult(granted=None, stale_tcc_likely=False)


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort SIGTERM-then-SIGKILL teardown. Never raises."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        log.debug("[mac_permissions] proc.terminate raised", exc_info=True)
    try:
        proc.wait(timeout=_PROBE_TERMINATE_GRACE_SECS)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        log.debug("[mac_permissions] proc.wait raised", exc_info=True)
    try:
        proc.kill()
        proc.wait(timeout=_PROBE_TERMINATE_GRACE_SECS)
    except Exception:
        log.debug("[mac_permissions] proc.kill raised", exc_info=True)


def prompt_notifications() -> TccPromptResult:
    """Call ``DesktopNotifierSync.request_authorisation`` — first call on
    macOS triggers the UNUserNotificationCenter dialog.

    Stale-TCC detection mirrors the mic + audio-capture paths: a UNN
    entry from a pre-v2.6.0 (ad-hoc-signed) Sayzo install whose code
    requirement no longer matches the current bundle silently denies
    without presenting UI. The same sub-500 ms-False fingerprint applies
    here. Returns a :class:`TccPromptResult`.
    """
    if sys.platform != "darwin":
        return TccPromptResult(granted=None, stale_tcc_likely=False)
    notifier = _get_notifier()
    if notifier is None:
        return TccPromptResult(granted=None, stale_tcc_likely=False)
    request_started = time.monotonic()
    try:
        granted = bool(notifier.request_authorisation())
    except Exception:
        log.warning(
            "[mac_permissions] request_authorisation failed", exc_info=True
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)
    elapsed = time.monotonic() - request_started
    stale_tcc_likely = (
        granted is False and elapsed < _STALE_TCC_THRESHOLD_SECS
    )
    log.info(
        "[mac_permissions] notifications TCC: user response → %s (elapsed=%.3fs, stale_tcc_likely=%s)",
        granted,
        elapsed,
        stale_tcc_likely,
    )
    return TccPromptResult(granted=granted, stale_tcc_likely=stale_tcc_likely)


def is_notification_authorised() -> Optional[bool]:
    """Non-prompting probe of current notification authorisation. Polled by
    the Notifications onboarding screen while the user toggles Sayzo on in
    System Settings, mirroring how Accessibility polls
    ``is_accessibility_trusted()``. Returns True / False / None (error)."""
    if sys.platform != "darwin":
        return None
    notifier = _get_notifier()
    if notifier is None:
        return None
    try:
        return bool(notifier.has_authorisation())
    except Exception:
        log.warning(
            "[mac_permissions] has_authorisation failed", exc_info=True
        )
        return None


def send_verification_notification() -> bool:
    """Fire a single test toast so the user can confirm notifications
    actually appear on their screen. ``request_authorisation`` returning
    True can lie when the bundle is misconfigured (signed but not notarised,
    AUMID drift, etc.); an actual toast hitting the screen is ground truth.

    Returns True on best-effort send success, False otherwise. Failures
    are logged but never propagated.
    """
    if sys.platform != "darwin":
        return False
    notifier = _get_notifier()
    if notifier is None:
        log.warning(
            "[mac_permissions] verification toast skipped — notifier unavailable"
        )
        return False
    log.info("[mac_permissions] verification toast: send begin")
    try:
        # has_authorisation is a quick sync probe; logging the result
        # alongside the send call lets us correlate "user clicked Test
        # but no toast" reports against whether UNN even thinks we have
        # rights at the moment of the send.
        try:
            authed = notifier.has_authorisation()
            log.info(
                "[mac_permissions] verification toast: has_authorisation=%s",
                authed,
            )
        except Exception:
            log.debug(
                "[mac_permissions] verification toast: has_authorisation failed",
                exc_info=True,
            )

        identifier = notifier.send(
            title="Sayzo notifications are on",
            message="You'll see prompts like this when Sayzo spots a meeting.",
        )
        log.info(
            "[mac_permissions] verification toast: send done id=%s", identifier
        )
        return True
    except Exception:
        log.warning(
            "[mac_permissions] send_verification_notification failed",
            exc_info=True,
        )
        return False


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


def open_accessibility_settings() -> None:
    _open(_ACCESSIBILITY_DEEPLINK)


def is_accessibility_trusted() -> bool:
    """Return True if Sayzo currently has Accessibility permission.

    Uses ``AXIsProcessTrustedWithOptions`` with an explicit options dict
    (``{kAXTrustedCheckOptionPrompt: False}``) rather than passing ``None``
    or calling ``AXIsProcessTrusted()``. Apple's headers document
    ``AXIsProcessTrustedWithOptions(NULL)`` as equivalent to the cached
    ``AXIsProcessTrusted()`` form, so passing NULL doesn't reliably flip
    once the user grants access — even though the prior comment claimed
    it did. Passing an explicit options dict is what the macOS sample
    code does and is the form most likely to re-read the TCC database.

    Even with a proper options dict, macOS does not always notify a
    long-running process when its Accessibility entry is added, so this
    can still return False after a successful grant. The setup window
    pairs this with a "Restart Sayzo" escape hatch (Accessibility.tsx)
    so the user is never stuck — a relaunched process always sees the
    correct trust state on startup.

    Returns False on non-darwin and on any binding failure (so the GUI
    keeps polling rather than silently passing on a bad import).
    """
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except Exception:
        log.debug(
            "[mac_permissions] AXIsProcessTrustedWithOptions unavailable",
            exc_info=True,
        )
        return False
    try:
        options = {kAXTrustedCheckOptionPrompt: False}
        return bool(AXIsProcessTrustedWithOptions(options))
    except Exception:
        log.debug(
            "[mac_permissions] AXIsProcessTrustedWithOptions call failed",
            exc_info=True,
        )
        return False
