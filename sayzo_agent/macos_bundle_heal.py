"""macOS bundle self-heal for DMG-drag-and-drop installs on managed Macs.

PyInstaller's macOS bundler ad-hoc-signs entries it puts in ``binaries``
but NOT files it adds via ``datas``. The Swift helpers ``audio-tap`` and
``audio-detect`` ship via ``datas`` and arrive on the user's Mac unsigned.
On Apple Silicon the dyld loader requires *some* signature to load any
Mach-O, and on MDM-managed Macs (Rippling / Jamf / Intune / etc.)
Gatekeeper SIGABRTs an unsigned-and-quarantined helper at subprocess
spawn time — before the helper executes its first instruction, so it
never reaches its ``AudioHardwareCreateProcessTap`` call and the user
gets stuck on the Audio Capture step of onboarding with no TCC prompt.

The terminal one-liner installer (``installer/install.sh``) handles this
at install time. Users who instead drag the .app out of the DMG in
Finder bypass that script entirely; we self-heal at parent startup so
both install paths reach the same working state. The parent already has
TCC clearance and write access to its own bundle by the time this runs,
so no privilege escalation is required.

Best-effort: every failure is logged but never blocks the agent from
starting. No-op on Linux / Windows and on dev (non-frozen) runs.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# PyInstaller-data helpers we know ship unsigned. Paths relative to the
# .app bundle root.
_HELPER_RELATIVE_PATHS = (
    "Contents/Frameworks/sayzo_agent/capture/audio-tap/audio-tap",
    "Contents/Frameworks/sayzo_agent/arm/audio-detect/audio-detect",
)

_SUBPROCESS_TIMEOUT_SECS = 5.0


def _resolve_bundle() -> Optional[Path]:
    """Return the absolute path of the running .app bundle, or None if
    we're not running from a frozen macOS bundle.

    In a PyInstaller .app the layout is:
        /Applications/Sayzo.app/Contents/MacOS/sayzo-agent  ← sys.executable
                                /Contents/                    ← parents[1]
                                /Sayzo.app                    ← parents[2]
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if len(exe.parents) < 3:
        return None
    candidate = exe.parents[2]
    if candidate.suffix != ".app":
        return None
    return candidate


def heal_bundle() -> None:
    """Strip ``com.apple.quarantine`` recursively and ad-hoc-sign the
    Swift helpers. Idempotent. Safe to call on every startup."""
    if sys.platform != "darwin":
        return
    bundle = _resolve_bundle()
    if bundle is None:
        return

    log.info("[mac_heal] healing bundle %s", bundle)

    # 1. Strip com.apple.quarantine recursively from every file in the
    #    bundle. macOS applies this xattr when the user copies a file out
    #    of a mounted DMG, and Gatekeeper enforces against it on every
    #    subprocess spawn. No-op when already clean.
    try:
        result = subprocess.run(
            ["xattr", "-cr", str(bundle)],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            log.warning(
                "[mac_heal] xattr -cr exited %d, stderr=%r",
                result.returncode,
                result.stderr.decode("utf-8", errors="replace")[:200],
            )
        else:
            log.info("[mac_heal] xattr -cr ok")
    except Exception:
        log.warning("[mac_heal] xattr -cr failed", exc_info=True)

    # 2. Ad-hoc sign the Swift helpers individually. We deliberately do
    #    NOT --deep sign the whole bundle: the parent agent is already
    #    running, and replacing its on-disk signature mid-execution can
    #    surprise hardened-runtime / library-validation checks on macOS
    #    versions that enforce them. Targeting just the two known
    #    PyInstaller-data helpers gets the user unstuck without that risk.
    for rel in _HELPER_RELATIVE_PATHS:
        helper = bundle / rel
        if not helper.exists():
            log.info("[mac_heal] %s missing, skipping", rel)
            continue
        try:
            result = subprocess.run(
                ["codesign", "--force", "--sign", "-", str(helper)],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
            )
            if result.returncode != 0:
                log.warning(
                    "[mac_heal] codesign %s exited %d, stderr=%r",
                    helper.name,
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace")[:200],
                )
            else:
                log.info("[mac_heal] codesign %s ok", helper.name)
        except Exception:
            log.warning(
                "[mac_heal] codesign %s failed", helper.name, exc_info=True
            )
