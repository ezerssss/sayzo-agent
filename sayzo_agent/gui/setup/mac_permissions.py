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
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
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

# Threshold for the "dialog never appeared" fingerprint used by
# `prompt_audio_capture`. A False arriving from the audio-tap binary
# in <500 ms is the system pre-rejecting the request without ever
# presenting UI. Possible causes (most likely first as of v2.7.4):
#
#   1. Missing Hardened-Runtime entitlement
#      (`com.apple.security.cs.disable-library-validation` or the
#      service-specific `com.apple.security.device.audio-input`). This
#      is what bit us across v2.6.0 → v2.7.3; fixed by wiring
#      `installer/macos/entitlements.plist` into the codesign step.
#   2. Missing usage description in Info.plist
#      (NSAudioCaptureUsageDescription).
#   3. Genuine orphan TCC entry whose code requirement no longer
#      matches the current bundle (rare, fixable with `tccutil reset
#      AudioCapture com.sayzo.agent`).
#
# Naming: `stale_tcc_likely` is the wire-format flag name — kept for
# back-compat with the bridge JSON contract, even though as of v2.7.4
# "dialog blocked / never appeared" is more accurate. See
# project_macos_silent_tcc_deny.md memory for the full history of
# wrong theories shipped under this name (v2.7.1 → v2.7.3).
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
    returns. ``stale_tcc_likely`` is True when the heuristic fingerprints
    a "dialog never appeared" condition — the GUI uses that flag to swap
    the generic "blocked" message for targeted recovery copy.

    The two prompt paths fingerprint differently:
      - **prompt_microphone** (AVFoundation
        ``requestAccessForMediaType:completionHandler:``, v2.7.8+):
        flags ``stale_tcc_likely`` when the completion block never
        fires within the timeout — the fingerprint of no dialog
        actually appearing on screen (Apple guarantees the completion
        fires whenever the user makes a decision).
      - **prompt_audio_capture** (Swift `audio-tap` helper, Process Taps
        API): flags ``stale_tcc_likely`` when the binary exits 77 in
        under :data:`_STALE_TCC_THRESHOLD_SECS` — same intuition, since
        no human can read a dialog and click that fast.

    Naming history (kept for back-compat with the bridge JSON contract):
    ``stale_tcc_likely`` was originally added in v2.7.1 under the theory
    that orphan TCC entries from pre-v2.6.0 ad-hoc-signed installs were
    silent-denying. v2.7.4 established the actual root cause was a
    missing Hardened-Runtime entitlement chain at the codesign step
    (``installer/macos/entitlements.plist`` wasn't wired into CI's
    ``codesign --entitlements``). The fingerprint detection itself is
    still accurate — both an orphan-CR entry AND a missing entitlement
    cause the same "dialog never appears" symptom — but the field is
    better understood as "the dialog was blocked by something at the OS
    level." See ``project_macos_silent_tcc_deny.md`` memory for the
    full timeline.
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
_ACCESSIBILITY_DEEPLINK = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

def _log_bundle_info_plist_once() -> None:
    """One-shot diagnostic: log the bundle's actual Info.plist values so we
    can tell — from a user's agent.log — whether the TCC failure is a
    bundle/build problem (usage-description key missing) or a stale-TCC
    problem (key present, request still silently denied).

    Surfaces three categories of "TCC dialog never appears":

    1. **`LSBackgroundOnly=True`** — PyInstaller's
       `building/osx.py` defaults this to True whenever the EXE block
       has `console=True` (we need that for CLI commands). macOS treats
       LSBackgroundOnly apps as agent apps that don't show UI — TCC
       refuses to render the dialog. v2.7.3 added an explicit override
       to `sayzo-agent.spec`'s `info_plist` setting it to False; this
       log line lets us verify the override actually landed in the
       built bundle.
    2. **Missing usage descriptions** —
       https://developer.apple.com/documentation/BundleResources/Information-Property-List/NSMicrophoneUsageDescription
       AVFoundation rejects at pre-flight, dialog never appears.
    3. **Null `CFBundleDisplayName`** — Apple Forum 30364: "the system
       will never know what app is asking for a permission." PyInstaller
       defaults this to the appname so it's almost always present, but
       we log it to be sure.
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

    # LSBackgroundOnly is the key field — PyInstaller's BUNDLE() default
    # sets it to True whenever console=True on the EXE block (which we
    # need for CLI commands), and a True value tells macOS the app is
    # an "agent app" that won't show UI — including TCC dialogs.
    # v2.7.3 added an explicit override to spec.info_plist setting this
    # to False; this diagnostic line lets us verify the override actually
    # landed in the shipped Info.plist (sometimes a stale build cache
    # silently keeps a previous value).
    log.info(
        "[mac_permissions] bundle Info.plist diagnostic: "
        "path=%s bundle_id=%r executable=%r LSUIElement=%r LSBackgroundOnly=%r "
        "CFBundleDisplayName=%r CFBundleName=%r",
        bundle_path,
        info.get("CFBundleIdentifier"),
        info.get("CFBundleExecutable"),
        info.get("LSUIElement"),
        info.get("LSBackgroundOnly"),
        info.get("CFBundleDisplayName"),
        info.get("CFBundleName"),
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

    Originally added in v2.7.1 under the (wrong) theory that the macOS
    silent-deny was caused by orphan TCC entries from pre-v2.6.0 ad-hoc-
    signed installs. v2.7.4 established the actual root cause was a
    missing Hardened-Runtime entitlement, which ``tccutil reset`` doesn't
    fix. The helper is kept anyway because it's still useful as a manual
    escape hatch — Jhoanna's machine had 4 stale entries that the reset
    cleaned up — and because the preemptive call before the requestAccess
    in :func:`prompt_microphone` is harmless when no entries exist.

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

    Pairs trigger and observation through the same AVFoundation API
    (``requestAccessForMediaType:completionHandler:``). The completion
    block fires after the user picks Allow / Don't Allow. Trigger via
    HAL + observe via AVFoundation looks symmetric but burns on
    AVFoundation's per-process status cache — see memory
    ``project_macos_silent_tcc_deny.md`` for the saga.

    Returns a :class:`TccPromptResult`. ``stale_tcc_likely=True`` only
    on the completion-timeout path, fingerprinting "dialog never
    presented" (missing usage description, entitlement drift, etc.).
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

    # Defensive preempt — clears any CR-mismatched orphan entry from a
    # prior Sayzo install that would otherwise force-deny without UI.
    # No-op when status is genuinely NotDetermined.
    _tccutil_reset_service(_TCC_SERVICE_MICROPHONE)

    log.info(
        "[mac_permissions] microphone TCC: calling requestAccessForMediaType:completionHandler:"
    )

    fut: Future[bool] = Future()

    def _completion(granted: bool) -> None:
        fut.set_result(bool(granted))

    request_started = time.monotonic()
    try:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, _completion
        )
    except Exception:
        log.warning(
            "[mac_permissions] requestAccessForMediaType_completionHandler_ raised",
            exc_info=True,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=False)

    try:
        granted = fut.result(timeout=_TCC_REQUEST_TIMEOUT_SECS)
    except FutureTimeoutError:
        log.warning(
            "[mac_permissions] microphone TCC: requestAccess timed out after %.0fs — "
            "completion never fired (dialog likely never presented)",
            _TCC_REQUEST_TIMEOUT_SECS,
        )
        return TccPromptResult(granted=None, stale_tcc_likely=True)

    log.info(
        "[mac_permissions] microphone TCC: requestAccess completion → %s (elapsed=%.3fs)",
        granted,
        time.monotonic() - request_started,
    )
    return TccPromptResult(granted=granted, stale_tcc_likely=False)


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
