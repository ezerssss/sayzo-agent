"""Shared click-to-record shortcut capture widget.

Used by both the Settings window (``gui/settings_window.py``) and the
first-run onboarding walkthrough (``onboarding.py``). A tkinter
``ttk.Frame`` that shows the current binding, a **Change...** button
that enters capture mode, and optionally an inline **Save** button.

Visual style comes from ``gui/theme.py`` — colors, fonts, and spacing
match the installer so the widget blends into every window that hosts it.

Capture semantics:

* Press **Change...** — the widget binds ``<KeyPress>`` / ``<KeyRelease>``
  on the toplevel window to track held modifiers.
* Press any non-modifier key while at least one modifier is held — the
  composed binding (``"ctrl+alt+s"`` format) is validated via
  :func:`validate_binding`; on success the widget exits capture mode.
* Press **Esc** — cancels capture and reverts to the previous value.

Callers integrate via:

* ``field.get_binding()`` — current value (original or user's new pick).
* ``on_save(binding) -> Optional[str]`` — optional Save-button callback.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from ..arm.hotkey import humanize_binding, validate_binding
from . import theme
from .theme import (
    BG,
    BORDER,
    INK,
    MUTED,
    PAD_LG,
    PAD_MD,
    PAD_SM,
    PAD_XS,
)
from .widgets import RoundedButton, RoundedFrame


# Modifier-keysym → canonical modifier name matching our stored binding
# format. Covers the names tkinter emits on Windows, macOS and Linux.
_MODIFIER_KEYSYMS: dict[str, str] = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Meta_L": "cmd",
    "Meta_R": "cmd",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Command": "cmd",
}


class ShortcutCaptureField(ttk.Frame):
    """Hotkey pill + Change button + optional Save button + inline status.

    Parameters:
        parent: tkinter parent widget.
        initial_binding: binding to display on open (e.g. ``"ctrl+alt+s"``).
        on_save: optional ``(binding: str) -> Optional[str]`` — when given,
            an inline **Save** button appears. Return ``None`` on success
            or an error string to surface inline.
    """

    def __init__(
        self,
        parent: tk.Widget,
        initial_binding: str,
        *,
        on_save: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        super().__init__(parent, style="Sayzo.TFrame")
        self._binding = initial_binding
        self._on_save = on_save
        self._capturing = False
        self._held_modifiers: set[str] = set()
        self._pending_binding: Optional[str] = None

        self._display_var = tk.StringVar(value=humanize_binding(initial_binding))
        self._status_var = tk.StringVar()
        self._status_style = tk.StringVar(value="Muted.Sayzo.TLabel")

        row = ttk.Frame(self, style="Sayzo.TFrame")
        row.pack(fill="x")

        # Shortcut "pill" — a rounded bordered frame with the binding text
        # inside. Shares the same corner radius as the buttons.
        self._pill = RoundedFrame(
            row, padx=PAD_MD, pady=PAD_SM - 1,
            fill=BG, outline=BORDER, outline_width=1,
        )
        self._pill.pack(side="left")
        self._display_label = tk.Label(
            self._pill.inner,
            textvariable=self._display_var,
            background=BG, foreground=INK,
            font=theme.FONT_BODY_BOLD,
        )
        self._display_label.pack()
        self._pill.fit()

        self._change_btn = RoundedButton(
            row, "Change...",
            command=self._start_capture,
            variant="secondary",
        )
        self._change_btn.pack(side="left", padx=(PAD_SM, 0))

        if on_save is not None:
            self._save_btn = RoundedButton(
                row, "Save",
                command=self._save,
                variant="primary",
                state="disabled",
            )
            self._save_btn.pack(side="left", padx=(PAD_SM, 0))
        else:
            self._save_btn = None

        # Status row — re-uses the theme's muted / error / success label
        # styles; we swap the style when the tone of the message changes.
        self._status_label = ttk.Label(
            self, textvariable=self._status_var,
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        )
        self._status_label.pack(anchor="w", pady=(PAD_SM, 0))

    # ---- public API ---------------------------------------------------

    def get_binding(self) -> str:
        """Return the currently-selected binding — the user's new pick if
        they completed a capture this session, otherwise the initial."""
        return self._pending_binding or self._binding

    def set_initial_binding(self, binding: str) -> None:
        """Reset the displayed binding + clear any pending edit."""
        self._binding = binding
        self._pending_binding = None
        self._display_var.set(humanize_binding(binding))
        if self._save_btn is not None:
            self._save_btn.configure(state="disabled")
        self._set_status("", "Muted.Sayzo.TLabel")

    def set_status(self, text: str, *, tone: str = "muted") -> None:
        """Surface a status message. ``tone`` is one of ``"muted"``,
        ``"error"``, ``"success"``."""
        style = {
            "muted": "Muted.Sayzo.TLabel",
            "error": "Error.Sayzo.TLabel",
            "success": "Success.Sayzo.TLabel",
        }.get(tone, "Muted.Sayzo.TLabel")
        self._set_status(text, style)

    # ---- capture lifecycle --------------------------------------------

    def _set_status(self, text: str, style: str) -> None:
        self._status_var.set(text)
        self._status_label.configure(style=style)

    def _start_capture(self) -> None:
        self._capturing = True
        self._held_modifiers.clear()
        self._pending_binding = None
        self._set_status(
            "Press a key combination... (Esc to cancel)",
            "Muted.Sayzo.TLabel",
        )
        self._change_btn.configure(state="disabled")
        if self._save_btn is not None:
            self._save_btn.configure(state="disabled")
        root = self.winfo_toplevel()
        root.bind("<KeyPress>", self._on_key_press)
        root.bind("<KeyRelease>", self._on_key_release)
        root.focus_set()
        # Highlight the pill so the user knows it's live.
        self._pill.set_outline(theme.ACCENT)

    def _stop_capture(self) -> None:
        self._capturing = False
        root = self.winfo_toplevel()
        root.unbind("<KeyPress>")
        root.unbind("<KeyRelease>")
        self._change_btn.configure(state="normal")
        self._pill.set_outline(BORDER)

    def _revert_display(self) -> None:
        self._display_var.set(humanize_binding(self._binding))
        self._pending_binding = None
        if self._save_btn is not None:
            self._save_btn.configure(state="disabled")

    def _on_key_press(self, event: tk.Event) -> None:
        if not self._capturing:
            return
        keysym = event.keysym
        if keysym == "Escape":
            self._stop_capture()
            self._revert_display()
            self._set_status("", "Muted.Sayzo.TLabel")
            return
        canonical = _MODIFIER_KEYSYMS.get(keysym)
        if canonical is not None:
            self._held_modifiers.add(canonical)
            return
        if not self._held_modifiers:
            self._set_status(
                "Please include at least one modifier (Ctrl, Alt, Shift, ⌘).",
                "Error.Sayzo.TLabel",
            )
            return
        key = keysym.lower()
        binding = "+".join(sorted(self._held_modifiers) + [key])
        err = validate_binding(binding)
        if err is not None:
            self._set_status(err, "Error.Sayzo.TLabel")
            return
        self._pending_binding = binding
        self._display_var.set(humanize_binding(binding))
        if self._save_btn is not None:
            self._save_btn.configure(state="normal")
            self._set_status("Press Save to apply.", "Muted.Sayzo.TLabel")
        else:
            self._set_status("", "Muted.Sayzo.TLabel")
        self._stop_capture()

    def _on_key_release(self, event: tk.Event) -> None:
        if not self._capturing:
            return
        canonical = _MODIFIER_KEYSYMS.get(event.keysym)
        if canonical is not None:
            self._held_modifiers.discard(canonical)

    def _save(self) -> None:
        if self._on_save is None or self._pending_binding is None:
            return
        err = self._on_save(self._pending_binding)
        if err is not None:
            self._set_status(err, "Error.Sayzo.TLabel")
            return
        self._binding = self._pending_binding
        self._pending_binding = None
        if self._save_btn is not None:
            self._save_btn.configure(state="disabled")
        self._set_status("Saved.", "Success.Sayzo.TLabel")
