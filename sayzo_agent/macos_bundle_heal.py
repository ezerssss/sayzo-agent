"""macOS bundle self-heal for DMG-drag-and-drop installs.

Production builds are Developer-ID-signed in CI with ``codesign --deep``,
which recursively signs every Mach-O inside the bundle — including the
Swift helpers ``audio-tap`` and ``audio-detect`` that PyInstaller adds
via ``datas`` (those entries used to arrive unsigned, since PyInstaller
only ad-hoc-signs ``binaries``-class entries). The DMG is then
notarized and stapled. On a properly-built release this module's
``codesign`` step finds each helper already validly signed and skips —
the only work it does is the ``xattr -cr`` quarantine strip below,
which is harmless and useful on managed Macs (Rippling / Jamf / Intune)
where Gatekeeper assesses the quarantine flag on every subprocess
spawn.

The ad-hoc ``codesign --sign -`` fallback is retained for unsigned dev
builds run locally (``pyinstaller sayzo-agent.spec`` without the CI
signing step). On those, the Swift helpers arrive unsigned and would
SIGABRT under Apple Silicon's mandatory-signature loader. We re-sign
ad-hoc ONLY when the existing signature is missing or invalid — never
on a Developer-ID-signed helper, since replacing a valid signature
with an ad-hoc one would break the parent bundle's notarization
assessment.

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

    # 2. Ad-hoc sign the Swift helpers individually IF they aren't
    #    already validly signed. On a Developer-ID-signed + notarized
    #    production build, ``codesign --deep`` in CI has already signed
    #    these helpers, so ``codesign --verify`` succeeds and we skip
    #    the ad-hoc resign. Re-signing a Developer-ID-signed binary
    #    with ``--sign -`` would replace the notarization-assessable
    #    signature with an ad-hoc one and break Gatekeeper acceptance
    #    of the bundle.
    #
    #    For unsigned dev builds (no CI signing), the verify call
    #    fails and we fall through to the ad-hoc sign path so the
    #    helpers can spawn under Apple Silicon's mandatory-signature
    #    loader.
    #
    #    We deliberately do NOT --deep sign the whole bundle either:
    #    the parent agent is already running, and replacing its on-disk
    #    signature mid-execution can surprise hardened-runtime /
    #    library-validation checks on macOS versions that enforce them.
    for rel in _HELPER_RELATIVE_PATHS:
        helper = bundle / rel
        if not helper.exists():
            log.info("[mac_heal] %s missing, skipping", rel)
            continue
        try:
            verify = subprocess.run(
                ["codesign", "--verify", "--strict", str(helper)],
                capture_output=True,
                timeout=_SUBPROCESS_TIMEOUT_SECS,
            )
            if verify.returncode == 0:
                log.info(
                    "[mac_heal] codesign %s already valid, skipping resign",
                    helper.name,
                )
                continue
        except Exception:
            log.warning(
                "[mac_heal] codesign --verify %s failed; will attempt ad-hoc resign",
                helper.name,
                exc_info=True,
            )

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
                log.info("[mac_heal] codesign %s ok (ad-hoc)", helper.name)
        except Exception:
            log.warning(
                "[mac_heal] codesign %s failed", helper.name, exc_info=True
            )
