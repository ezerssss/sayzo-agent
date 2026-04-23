"""First-run onboarding walkthrough — tkinter, matches installer theme.

Two platform flows share the same widget library and theme as the Settings
window (``gui/theme.py`` + ``gui/shortcut_capture.py``):

* **Windows** (2 pages):
    1. Pick your start-recording shortcut.
    2. You're all set.

* **macOS** (5 steps — copy and order per ``installer/copy_draft.md`` §3):
    1. Microphone access.
    2. System Audio Recording.
    3. Accessibility.
    4. Automation (browsers) — per-installed-browser AppleScript probe.
    5. Pick your start-recording shortcut.

State persistence: ``data_dir/onboarding.json`` is written once the user
completes the flow. If they close the window early, the flag isn't
written and the walkthrough re-opens on the next launch.

The tray's **Reopen setup** menu item opens this walkthrough any time,
regardless of the flag (users who already finished can still revisit the
permissions or tweak their shortcut).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

from . import settings_store
from .arm.hotkey import humanize_binding
from .gui import theme
from .gui.shortcut_capture import ShortcutCaptureField
from .gui.widgets import RoundedButton
from .gui.theme import (
    ACCENT,
    BG,
    BORDER,
    INK,
    MUTED,
    PAD_LG,
    PAD_MD,
    PAD_SM,
    PAD_XL,
    PAD_XXL,
    SURFACE,
    apply_sayzo_icon,
    apply_sayzo_theme,
)

if TYPE_CHECKING:
    from .arm.controller import ArmController
    from .config import Config

log = logging.getLogger(__name__)


_FLAG_NAME = "onboarding.json"


def has_onboarded(data_dir: Path) -> bool:
    return (data_dir / _FLAG_NAME).exists()


def _mark_onboarded(data_dir: Path) -> None:
    path = data_dir / _FLAG_NAME
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"completed_at": None}), encoding="utf-8")
    except OSError:
        log.debug("[onboarding] flag write failed (non-fatal)", exc_info=True)


def open_onboarding_window(cfg: "Config", arm: "ArmController") -> bool:
    """Blocking: open the walkthrough, return True if user completed it.

    Safe to call from a worker thread. Swallows exceptions and logs them —
    a crashed walkthrough must never kill the agent.
    """
    try:
        app = _OnboardingApp(cfg, arm)
        return app.run()
    except Exception:
        log.warning("[onboarding] window crashed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class _OnboardingApp:
    """Owns the root + step sequence. One instance per open."""

    WINDOW_SIZE = (720, 540)
    MIN_SIZE = (640, 460)

    def __init__(self, cfg: "Config", arm: "ArmController") -> None:
        self._cfg = cfg
        self._arm = arm
        self._root = tk.Tk()
        self._root.title("Sayzo — Setup")
        self._root.geometry(f"{self.WINDOW_SIZE[0]}x{self.WINDOW_SIZE[1]}")
        self._root.minsize(*self.MIN_SIZE)
        apply_sayzo_theme(self._root)
        apply_sayzo_icon(self._root)

        self._completed = False
        self._step_idx = 0
        self._steps: list[Callable[[tk.Widget], None]] = []
        self._container: Optional[ttk.Frame] = None

        self._configure_steps()
        self._build_shell()
        self._render()

    def run(self) -> bool:
        self._root.mainloop()
        return self._completed

    # ---- step selection --------------------------------------------------

    def _configure_steps(self) -> None:
        if sys.platform == "darwin":
            self._steps = [
                self._step_mac_microphone,
                self._step_mac_audio_capture,
                self._step_mac_accessibility,
                self._step_mac_automation,
                self._step_mac_shortcut,
            ]
        else:
            self._steps = [
                self._step_win_shortcut,
                self._step_win_done,
            ]

    def _total_steps(self) -> int:
        return len(self._steps)

    def _step_label(self) -> str:
        return f"Step {self._step_idx + 1} of {self._total_steps()}"

    # ---- window shell + step rendering -----------------------------------

    def _build_shell(self) -> None:
        """The outer padding frame — persistent across step renders."""
        self._container = ttk.Frame(self._root, style="Sayzo.TFrame",
                                    padding=(PAD_XXL, PAD_XXL))
        self._container.pack(fill="both", expand=True)

    def _render(self) -> None:
        """Clear + rebuild the container with the current step."""
        assert self._container is not None
        for child in self._container.winfo_children():
            child.destroy()
        self._steps[self._step_idx](self._container)

    def _advance(self) -> None:
        if self._step_idx + 1 >= len(self._steps):
            self._finish()
            return
        self._step_idx += 1
        self._render()

    def _finish(self) -> None:
        self._completed = True
        try:
            _mark_onboarded(self._cfg.data_dir)
        except Exception:
            log.debug("[onboarding] mark_onboarded failed", exc_info=True)
        try:
            self._root.destroy()
        except Exception:
            pass

    # ---- step scaffold helpers ------------------------------------------

    def _render_header(
        self, parent: tk.Widget, title: str, subtitle: Optional[str] = None,
        step: Optional[str] = None,
    ) -> None:
        header = ttk.Frame(parent, style="Sayzo.TFrame")
        header.pack(fill="x")
        if step:
            ttk.Label(
                header, text=step.upper(), style="Step.Sayzo.TLabel",
            ).pack(anchor="w", pady=(0, PAD_SM))
        ttk.Label(
            header, text=title, style="H1.Sayzo.TLabel",
        ).pack(anchor="w")
        if subtitle:
            ttk.Label(
                header, text=subtitle, style="Muted.Sayzo.TLabel",
                wraplength=540, justify="left",
            ).pack(anchor="w", pady=(PAD_SM, 0))

    def _render_body(self, parent: tk.Widget) -> ttk.Frame:
        """Create the middle content area and return it so the step can
        pack into it."""
        body = ttk.Frame(parent, style="Sayzo.TFrame")
        body.pack(fill="both", expand=True, pady=(PAD_XL, PAD_XL))
        return body

    def _render_footer(
        self, parent: tk.Widget, *,
        primary_text: str,
        primary_action: Callable[[], None],
        secondary_text: Optional[str] = None,
        secondary_action: Optional[Callable[[], None]] = None,
    ) -> None:
        """Right-aligned button row at the bottom of the step."""
        footer = ttk.Frame(parent, style="Sayzo.TFrame")
        footer.pack(fill="x", side="bottom")

        # Primary packed right, secondary to its left (so reading order is
        # "secondary — primary" left-to-right).
        RoundedButton(
            footer, primary_text,
            command=primary_action,
            variant="primary",
        ).pack(side="right")

        if secondary_text is not None and secondary_action is not None:
            RoundedButton(
                footer, secondary_text,
                command=secondary_action,
                variant="ghost",
            ).pack(side="right", padx=(0, PAD_SM))

    # ---------------------------------------------------------------------
    # macOS steps
    # ---------------------------------------------------------------------

    def _step_mac_microphone(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="Sayzo needs to hear you",
            subtitle="We'll only record when you ask — either with your "
                     "keyboard shortcut, or after you say yes to an on-screen "
                     "prompt. This permission just lets Sayzo open the "
                     "microphone at that moment.",
            step=self._step_label(),
        )
        self._render_body(parent)
        self._render_footer(
            parent,
            primary_text="Grant",
            primary_action=lambda: self._grant_mac("mic"),
            secondary_text="Skip for now",
            secondary_action=self._advance,
        )

    def _step_mac_audio_capture(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="And the other side of your meetings",
            subtitle="So Sayzo can transcribe what the other person says, "
                     "not just you. We don't read or record anything visible "
                     "on your screen — just audio coming through your "
                     "speakers.",
            step=self._step_label(),
        )
        self._render_body(parent)
        self._render_footer(
            parent,
            primary_text="Grant",
            primary_action=lambda: self._grant_mac("audio_capture"),
            secondary_text="Skip for now",
            secondary_action=self._advance,
        )

    def _step_mac_accessibility(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="Let the shortcut work anywhere",
            subtitle="Without this, Sayzo's global shortcut won't work when "
                     "another app is focused. You'll have to open the tray "
                     "menu to start recording. You can grant this later from "
                     "System Settings.",
            step=self._step_label(),
        )
        self._render_body(parent)
        self._render_footer(
            parent,
            primary_text="Open System Settings",
            primary_action=lambda: self._grant_mac("accessibility"),
            secondary_text="Skip for now",
            secondary_action=self._advance,
        )

    def _step_mac_automation(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="Know when you're in a web meeting",
            subtitle="So Sayzo can tell you're in Google Meet or Teams on the "
                     "web, instead of just browsing. We only read the current "
                     "tab's URL — never its contents.",
            step=self._step_label(),
        )
        self._render_body(parent)
        self._render_footer(
            parent,
            primary_text="Grant (per browser)",
            primary_action=lambda: self._grant_mac("automation"),
            secondary_text="Skip for now",
            secondary_action=self._advance,
        )

    def _step_mac_shortcut(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="Last thing — pick your shortcut",
            subtitle="Press this anywhere to start capturing a meeting, or "
                     "stop one in progress. You can change it anytime in "
                     "Sayzo's settings.",
            step=self._step_label(),
        )
        body = self._render_body(parent)
        field = ShortcutCaptureField(
            body, self._arm.current_hotkey,
        )
        field.pack(anchor="w")

        def _done() -> None:
            chosen = field.get_binding()
            if chosen != self._arm.current_hotkey:
                err = self._save_hotkey(chosen)
                if err is not None:
                    field.set_status(err, tone="error")
                    return
            self._finish()

        self._render_footer(
            parent,
            primary_text="Done — take me to the tray",
            primary_action=_done,
        )

    # ---------------------------------------------------------------------
    # Windows steps
    # ---------------------------------------------------------------------

    def _step_win_shortcut(self, parent: tk.Widget) -> None:
        self._render_header(
            parent,
            title="Pick your start-recording shortcut",
            subtitle="Press this anywhere on your computer to start capturing "
                     "a meeting, or to stop a capture in progress. You can "
                     "change it later in Sayzo's settings.",
            step=self._step_label(),
        )
        body = self._render_body(parent)
        field = ShortcutCaptureField(body, self._arm.current_hotkey)
        field.pack(anchor="w")

        def _advance_if_valid() -> None:
            chosen = field.get_binding()
            if chosen != self._arm.current_hotkey:
                err = self._save_hotkey(chosen)
                if err is not None:
                    field.set_status(err, tone="error")
                    return
            self._advance()

        self._render_footer(
            parent,
            primary_text="Looks good →",
            primary_action=_advance_if_valid,
        )

    def _step_win_done(self, parent: tk.Widget) -> None:
        hotkey_display = humanize_binding(self._arm.current_hotkey)
        self._render_header(
            parent,
            title="You're all set",
            subtitle=f"Sayzo is running in your system tray. Press "
                     f"{hotkey_display} anytime to start capturing a meeting. "
                     "We'll also ask you when we notice you're in a meeting "
                     "app like Zoom, Teams, Discord, or Google Meet.",
            step=self._step_label(),
        )
        self._render_body(parent)
        self._render_footer(
            parent,
            primary_text="Got it",
            primary_action=self._finish,
        )

    # ---------------------------------------------------------------------
    # Actions
    # ---------------------------------------------------------------------

    def _save_hotkey(self, binding: str) -> Optional[str]:
        """Apply the binding live + persist. Returns an error string or None."""
        err = self._arm.rebind_hotkey(binding)
        if err is not None:
            return err
        try:
            settings_store.save(
                self._cfg.data_dir, {"arm": {"hotkey": binding}},
            )
        except Exception:
            log.warning("[onboarding] persist hotkey failed", exc_info=True)
            return ("Saved to the running agent, but couldn't write to "
                    "user_settings.json.")
        return None

    def _grant_mac(self, key: str) -> None:
        """Surface the corresponding OS prompt, then advance regardless of
        the outcome (the user will see the result in System Settings)."""
        if sys.platform != "darwin":
            self._advance()
            return

        def worker() -> None:
            try:
                if key == "mic":
                    from .gui.setup import mac_permissions
                    mac_permissions.prompt_microphone()
                elif key == "audio_capture":
                    from .gui.setup import mac_permissions
                    mac_permissions.prompt_audio_capture()
                elif key == "accessibility":
                    subprocess.Popen([
                        "open",
                        "x-apple.systempreferences:com.apple.preference.security"
                        "?Privacy_Accessibility",
                    ])
                elif key == "automation":
                    _prompt_automation_for_installed_browsers()
            except Exception:
                log.warning("[onboarding] grant %s failed", key, exc_info=True)
            # Advance on the tk thread.
            try:
                self._root.after(0, self._advance)
            except Exception:
                pass

        threading.Thread(target=worker, name="onboarding-grant",
                         daemon=True).start()


# ---------------------------------------------------------------------------
# macOS Automation helper — probe installed browsers for tab-URL read
# permission. Each AppleScript call surfaces the Automation TCC prompt for
# that browser if the user hasn't decided yet.
# ---------------------------------------------------------------------------


_BROWSER_APPLESCRIPTS: list[tuple[str, str, str]] = [
    # (bundle path, AppleScript application name, short label for logs)
    ("/Applications/Google Chrome.app", "Google Chrome", "chrome"),
    ("/Applications/Safari.app", "Safari", "safari"),
    ("/Applications/Microsoft Edge.app", "Microsoft Edge", "edge"),
    ("/Applications/Arc.app", "Arc", "arc"),
    ("/Applications/Brave Browser.app", "Brave Browser", "brave"),
]


def _prompt_automation_for_installed_browsers() -> None:
    for path, app_name, label in _BROWSER_APPLESCRIPTS:
        if not Path(path).exists():
            continue
        script = (
            f'tell application "{app_name}" to '
            'get URL of active tab of front window'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=3.0,
            )
        except subprocess.TimeoutExpired:
            log.debug("[onboarding] automation probe timed out for %s", label)
        except OSError:
            log.debug("[onboarding] osascript missing", exc_info=True)
            return
