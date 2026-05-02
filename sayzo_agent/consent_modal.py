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
from typing import Literal

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

    script = (
        f'display dialog "{_escape_applescript(body)}" '
        f'with title "{_escape_applescript(title)}" '
        f'buttons {{"{_escape_applescript(no_label)}", '
        f'"{_escape_applescript(yes_label)}"}} '
        f'default button "{_escape_applescript(yes_label)}" '
        f'cancel button "{_escape_applescript(no_label)}" '
        f'giving up after {max(1, int(timeout_secs))} '
        'with icon note'
    )

    log.info(
        "[modal] consent: title=%r yes=%r no=%r timeout=%ss",
        title,
        yes_label,
        no_label,
        timeout_secs,
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
