"""Cross-platform consent dialogs (modals) for decisions the user MUST see.

Why a separate path from notifications
--------------------------------------

On macOS, banner-style notifications are filtered through the user's
Banner Style preference (System Settings → Notifications → Sayzo) and
through Focus / Do Not Disturb modes. A user with ``Banner Style:
None`` only sees notifications inside Notification Center — they have
to click the menu-bar clock to find them. For decisions Sayzo needs
the user to make right now ("Sayzo noticed you're in a Google Meet
call — coach this?"), that's a UX dead-end: the prompt is *technically*
visible, but in practice gets missed and times out, so the
auto-suggest feature silently never works.

Modals (this module) sidestep all of that:

* ``osascript display dialog`` is dispatched by the system-signed
  ``osascript`` binary — zero dependency on our bundle's signature,
  AUMID, codesign verdict, or notification permissions.
* Independent of Banner Style / Focus mode — a modal is a window,
  not a notification.
* Spawned as a subprocess so it can't block our main thread or
  pystray's run loop.
* Yes / No buttons render as actual buttons inline, no hover-to-reveal,
  no nested options, no "click the notification to see actions".

Trade-off: a modal steals focus while open. For decision-blocking
consents that's actually correct UX — you *should* notice that
Sayzo is asking you something. For ambient signals (capture saved,
welcome message), notifications stay the right tool — they don't
need acknowledgement.

Mapping in the agent
--------------------

* ``notifier.notify(...)`` (welcome, capture-saved, post-arm guidance,
  upload status) → notification, both platforms.
* ``notifier.ask_consent(...)`` (whitelist auto-detect, hotkey start,
  end-of-meeting, long-meeting check-in, meeting-ended-watcher) →
  on macOS: modal via this module. On Windows: notification (WinRT
  toast click-through is reliable enough that the modal isn't needed).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Literal, Optional

log = logging.getLogger(__name__)


ConsentResult = Literal["yes", "no", "timeout"]


def _escape_applescript(s: str) -> str:
    """Escape backslashes, quotes, and newlines for embedding inside
    an AppleScript string literal. Newlines become the two-character
    sequence ``\\n`` so the dialog renders multi-line bodies properly
    (AppleScript needs the literal escape sequence; a bare ``\\n``
    in the script source is interpreted as an end-of-statement).
    """
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
    )


def _sayzo_icns_path() -> Optional[str]:
    """Locate the bundled Sayzo ``.icns`` for use as the dialog icon.

    AppleScript's ``display dialog ... with icon`` accepts either one of
    the built-in glyphs (``note`` / ``caution`` / ``stop``) or a file
    reference to a ``.icns`` file. Built-in ``note`` shows a speech-
    bubble badge on top of ``osascript``'s generic script-runner icon —
    not Sayzo. Pointing at the app bundle's own ``.icns`` swaps that
    for the Sayzo logo.

    Lookup order:

    1. **Frozen ``.app`` bundle** — ``Sayzo.app/Contents/Resources/logo.icns``,
       resolved from ``sys.executable`` (``Contents/MacOS/sayzo-agent``).
       PyInstaller's ``BUNDLE(icon=...)`` step copies the source ``.icns``
       to ``Contents/Resources/<basename>`` (see ``sayzo-agent.spec:40``,
       which passes ``installer/assets/logo.icns``), so this is the
       canonical location in production builds.
    2. **Dev tree** — ``installer/assets/logo.icns`` if a developer has
       generated it locally. The repo only ships ``logo.png`` + ``logo.ico``;
       the ``.icns`` is produced by the macOS CI job (``iconutil`` step at
       ``.github/workflows/build.yml:123``), so a fresh dev checkout won't
       have one and we fall through to None — caller then uses
       ``with icon note``.

    Returns:
        Absolute POSIX path string, or ``None`` if no ``.icns`` is on disk.
    """
    if sys.platform != "darwin":
        return None

    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        try:
            # sys.executable in a frozen .app is .../Sayzo.app/Contents/MacOS/sayzo-agent
            macos_dir = Path(sys.executable).resolve().parent
            candidates.append(macos_dir.parent / "Resources" / "logo.icns")
        except (OSError, ValueError):
            pass

    # Dev fallback — sayzo_agent/consent_modal.py → repo root is one parent up.
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "installer" / "assets" / "logo.icns")

    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def consent_modal_macos(
    title: str,
    body: str,
    yes_label: str,
    no_label: str,
    timeout_secs: float,
    default_on_timeout: ConsentResult = "no",
) -> ConsentResult:
    """Show an AppleScript ``display dialog`` and return the user's choice.

    Synchronous — blocks the calling thread until the user clicks,
    presses Esc, or the dialog times out. ArmController already calls
    ``ask_consent`` in a thread executor (controller.py:1219-1225), so
    this blocking is contained and doesn't freeze the agent loop.

    Returns:
      * ``"yes"`` — user clicked the action button (yes_label)
      * ``"no"`` — user clicked the cancel button OR pressed Esc /
        Cmd-. (those map to the AppleScript "cancel button")
      * ``"timeout"`` — ``giving up after`` elapsed without input.
        Maps to ``default_on_timeout`` per the ask_consent contract.

    Returns ``default_on_timeout`` on any subprocess / parsing failure.
    """
    if sys.platform != "darwin":
        return default_on_timeout

    icns = _sayzo_icns_path()
    if icns is not None:
        # `(POSIX file "/abs/path.icns")` evaluates to a file reference
        # AppleScript can render the icon resource from. We escape the
        # path the same way as user-facing strings — paths under
        # `/Users/<name>/Applications/...` may contain `"` or `\` even
        # if it's vanishingly unlikely.
        icon_clause = f'with icon (POSIX file "{_escape_applescript(icns)}")'
    else:
        # Dev / unbundled run: fall back to the built-in note glyph rather
        # than show no icon at all.
        icon_clause = "with icon note"

    script = (
        f'display dialog "{_escape_applescript(body)}" '
        f'with title "{_escape_applescript(title)}" '
        f'buttons {{"{_escape_applescript(no_label)}", '
        f'"{_escape_applescript(yes_label)}"}} '
        f'default button "{_escape_applescript(yes_label)}" '
        f'cancel button "{_escape_applescript(no_label)}" '
        f'giving up after {max(1, int(timeout_secs))} '
        f'{icon_clause}'
    )

    log.info(
        "[modal] consent: title=%r yes=%r no=%r timeout=%ss icon=%s",
        title,
        yes_label,
        no_label,
        timeout_secs,
        icns or "<note>",
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_secs + 5.0,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "[modal] osascript subprocess timed out after %ss",
            timeout_secs + 5.0,
        )
        return default_on_timeout
    except FileNotFoundError:
        log.warning("[modal] osascript not found — cannot show modal")
        return default_on_timeout
    except Exception:
        log.warning("[modal] osascript subprocess failed", exc_info=True)
        return default_on_timeout

    output = result.stdout.strip()
    err_output = result.stderr.strip()
    log.info(
        "[modal] returncode=%d stdout=%r stderr=%r",
        result.returncode,
        output,
        err_output,
    )

    # Esc / Cmd-. with a "cancel button" set returns rc=1 + empty stdout.
    if result.returncode == 1 and not output:
        return "no"

    # Output format (single line, no quoting on field values):
    #     button returned:LABEL, gave up:<true|false>
    # or, when no ``giving up after`` is in effect:
    #     button returned:LABEL
    #
    # LABEL itself may contain commas — every "Yes, stop" / "Yes, start"
    # / "Yes, done" / "Yes, keep going" toast does. A previous regex
    # parser split on the first comma and captured just "Yes", which
    # never matched yes_label and silently fell back to default_on_timeout
    # — so e.g. "Yes, stop" on the disarm-confirm toast was parsed as
    # "no" and recording kept running. Strip the optional ``, gave up:``
    # trailing marker first (rsplit so a label-internal comma can't
    # eat it) and treat the residual as the literal label.
    prefix = "button returned:"
    if not output.startswith(prefix):
        log.warning(
            "[modal] could not parse osascript output: %r — returning default",
            output,
        )
        return default_on_timeout

    gave_up_marker = ", gave up:"
    button_section = output[len(prefix):]
    gave_up_value = ""
    if gave_up_marker in button_section:
        button_section, gave_up_value = button_section.rsplit(gave_up_marker, 1)

    if gave_up_value.strip().lower().startswith("true"):
        return "timeout"

    button = button_section.strip()
    if button == yes_label:
        return "yes"
    if button == no_label:
        return "no"
    log.warning(
        "[modal] unrecognised button label %r — returning default", button
    )
    return default_on_timeout


async def consent_modal_macos_async(
    title: str,
    body: str,
    yes_label: str,
    no_label: str,
    timeout_secs: float,
    default_on_timeout: ConsentResult = "no",
) -> ConsentResult:
    """Async wrapper — dispatches the synchronous osascript call to a
    thread executor so it doesn't block the calling asyncio loop."""
    if sys.platform != "darwin":
        return default_on_timeout
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        consent_modal_macos,
        title,
        body,
        yes_label,
        no_label,
        timeout_secs,
        default_on_timeout,
    )
