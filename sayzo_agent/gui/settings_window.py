"""Settings window for the running agent — five panes, tkinter-hosted.

Opened from the tray menu's "Settings..." click. Runs on a worker thread
hosted by ``_tray_bridge`` in ``__main__.py`` via ``loop.run_in_executor``;
tkinter's mainloop then drives the UI on that thread until the user closes
the window.

Visual language is kept in sync with the installer (``gui/webui``): same
white background, near-black text, Sayzo blue accent, slate-500 muted
text, slate-200 borders. Colors + fonts + spacing live in
``gui/theme.py``.

Panes (left sidebar navigation):

* **Shortcut** — current global hotkey + click-to-record Change button.
  On Save, calls :meth:`ArmController.rebind_hotkey` and persists via
  ``settings_store``.
* **Meeting Apps** — whitelist editor. Toggle / remove / add desktop and
  web meeting apps that Sayzo should auto-suggest recording for. Shows
  apps Sayzo has seen holding the mic as one-click suggestions.
* **Permissions** — macOS only. Per-permission rows with Re-request
  buttons wired to ``gui/setup/mac_permissions.py``. Windows shows a
  short "no permissions required" note.
* **Account** — reads the ``TokenStore`` and renders signed-in /
  signed-out state. Sign-in kicks off the PKCE flow on a worker thread.
* **Notifications** — master toggle + three sub-toggles. Mutations apply
  to the live ``Config`` and persist to ``user_settings.json``.

Consent and end-of-meeting toasts are NOT toggleable here — they are the
consent gateway and must always show.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from .. import settings_store
from ..arm import seen_apps as _seen_apps
from ..arm.detectors import BROWSER_PROCESS_NAMES
from ..config import DetectorSpec, default_detector_specs
from . import theme
from .shortcut_capture import ShortcutCaptureField
from .widgets import RoundedButton, RoundedFrame, SwitchToggle
from .theme import (
    ACCENT,
    ACCENT_TINT,
    BG,
    BORDER,
    ERROR,
    INK,
    MUTED,
    PAD_LG,
    PAD_MD,
    PAD_SM,
    PAD_XL,
    PAD_XS,
    PAD_XXL,
    SELECTED,
    SUBTLE,
    SUCCESS,
    SURFACE,
    apply_sayzo_icon,
    apply_sayzo_theme,
    make_divider,
)

if TYPE_CHECKING:
    from ..arm.controller import ArmController
    from ..config import Config

log = logging.getLogger(__name__)


PANE_NAMES = ("Shortcut", "Meeting Apps", "Permissions", "Account", "Notifications")


def open_settings_window(
    cfg: "Config",
    arm: "ArmController",
    *,
    pane: Optional[str] = None,
) -> None:
    """Blocking: opens the settings window and returns when the user closes it.

    Args:
        cfg: Agent config.
        arm: Live ArmController (for hotkey rebinding).
        pane: Optional pane name (e.g., ``"Account"``). When set, the
            window opens with that pane selected — used by the auth-expiry
            toast action button so the user lands directly on the
            sign-in surface.

    Safe to call from a worker thread. Tkinter pins widget access to the
    thread that created the root; every widget call below is local to
    this function's thread.
    """
    try:
        app = _SettingsApp(cfg, arm, initial_pane=pane)
        app.run()
    except Exception:
        log.warning("[settings] window crashed", exc_info=True)


class _SettingsApp:
    """Owns the tk root + the four panes. One instance per open."""

    WINDOW_SIZE = (880, 600)
    MIN_SIZE = (760, 500)
    SIDEBAR_WIDTH = 220

    def __init__(
        self,
        cfg: "Config",
        arm: "ArmController",
        *,
        initial_pane: Optional[str] = None,
    ) -> None:
        self._cfg = cfg
        self._arm = arm
        self._root = tk.Tk()
        self._root.title("Sayzo — Settings")
        self._root.geometry(f"{self.WINDOW_SIZE[0]}x{self.WINDOW_SIZE[1]}")
        self._root.minsize(*self.MIN_SIZE)
        apply_sayzo_theme(self._root)
        apply_sayzo_icon(self._root)

        # Honour initial_pane when it matches a known pane name — otherwise
        # fall back to the first pane. Case-insensitive to keep the caller
        # API forgiving (the tray passes a lowercased tag at the moment).
        start_pane = PANE_NAMES[0]
        if initial_pane is not None:
            for name in PANE_NAMES:
                if name.lower() == initial_pane.lower():
                    start_pane = name
                    break

        self._current_pane = tk.StringVar(value=start_pane)
        self._sidebar_items: dict[str, _SidebarItem] = {}
        self._panes: dict[str, _Pane] = {}
        self._content_frame: Optional[ttk.Frame] = None

        self._build_layout()

    def run(self) -> None:
        self._root.mainloop()

    # ---- layout ----------------------------------------------------------

    def _build_layout(self) -> None:
        sidebar = tk.Frame(
            self._root,
            width=self.SIDEBAR_WIDTH,
            background=SURFACE,
            highlightthickness=0,
            bd=0,
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Sidebar header.
        header = tk.Frame(sidebar, background=SURFACE)
        header.pack(fill="x", padx=PAD_XL, pady=(PAD_XL, PAD_XL))
        tk.Label(
            header, text="Sayzo",
            background=SURFACE, foreground=INK,
            font=theme.FONT_H2, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            header, text="Settings",
            background=SURFACE, foreground=MUTED,
            font=theme.FONT_SMALL, anchor="w",
        ).pack(anchor="w", pady=(PAD_XS, 0))

        # Pane nav items.
        nav = tk.Frame(sidebar, background=SURFACE)
        nav.pack(fill="x", padx=PAD_MD)
        for name in PANE_NAMES:
            item = _SidebarItem(
                nav, name,
                selected=lambda n=name: self._current_pane.get() == n,
                on_click=lambda n=name: self._select_pane(n),
            )
            item.pack(fill="x", pady=1)
            self._sidebar_items[name] = item

        # Vertical separator between sidebar and content.
        sep = tk.Frame(self._root, width=1, background=BORDER)
        sep.pack(side="left", fill="y")

        # Content area.
        self._content_frame = ttk.Frame(self._root, style="Sayzo.TFrame")
        self._content_frame.pack(side="left", fill="both", expand=True)

        self._panes = {
            "Shortcut": _ShortcutPane(self._content_frame, self._cfg, self._arm),
            "Meeting Apps": _MeetingAppsPane(self._content_frame, self._cfg, self._arm),
            "Permissions": _PermissionsPane(self._content_frame, self._cfg),
            "Account": _AccountPane(self._content_frame, self._cfg),
            "Notifications": _NotificationsPane(self._content_frame, self._cfg),
        }
        self._select_pane(PANE_NAMES[0])

    def _select_pane(self, name: str) -> None:
        self._current_pane.set(name)
        for item in self._sidebar_items.values():
            item.refresh()
        for key, pane in self._panes.items():
            if key == name:
                pane.show()
            else:
                pane.hide()


# ---------------------------------------------------------------------------
# Sidebar item — a clickable row with hover + selected states.
# ---------------------------------------------------------------------------


class _SidebarItem(tk.Frame):
    """A clickable nav row with hover + selected visual states."""

    _PAD_X = PAD_MD
    _PAD_Y = PAD_SM

    def __init__(self, parent, label: str, *, selected, on_click) -> None:
        super().__init__(parent, background=SURFACE, highlightthickness=0, bd=0)
        self._label_text = label
        self._is_selected = selected
        self._on_click = on_click

        self._accent = tk.Frame(self, width=3, background=SURFACE)
        self._accent.pack(side="left", fill="y")

        self._label = tk.Label(
            self, text=label, anchor="w",
            background=SURFACE, foreground=INK,
            font=theme.FONT_BODY,
            padx=self._PAD_X, pady=self._PAD_Y,
        )
        self._label.pack(side="left", fill="both", expand=True)

        for widget in (self, self._label, self._accent):
            widget.bind("<Button-1>", self._handle_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

        self.refresh()

    def _handle_click(self, _event) -> None:
        self._on_click()

    def _on_enter(self, _event) -> None:
        if self._is_selected():
            return
        self._set_bg(BORDER)  # subtle gray hover

    def _on_leave(self, _event) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._is_selected():
            self._set_bg(SELECTED)
            self._label.configure(
                foreground=INK, font=theme.FONT_BODY_BOLD,
            )
            self._accent.configure(background=ACCENT)
        else:
            self._set_bg(SURFACE)
            self._label.configure(
                foreground=INK, font=theme.FONT_BODY,
            )
            self._accent.configure(background=SURFACE)

    def _set_bg(self, color: str) -> None:
        # Color the row background (label + self) without touching the
        # accent strip — that's managed by refresh().
        self.configure(background=color)
        self._label.configure(background=color)


# ---------------------------------------------------------------------------
# Base + four panes
# ---------------------------------------------------------------------------


class _Pane:
    """Each pane owns a frame that's shown/hidden on selection."""

    def __init__(self, parent: tk.Widget) -> None:
        self._frame = ttk.Frame(parent, style="Sayzo.TFrame", padding=(PAD_XXL, PAD_XXL))

    def show(self) -> None:
        self._frame.pack(fill="both", expand=True)

    def hide(self) -> None:
        self._frame.pack_forget()


# ---- Shortcut pane ---------------------------------------------------------


class _ShortcutPane(_Pane):
    """Hotkey editor. Delegates to ``ShortcutCaptureField``; wires its Save
    callback to ``ArmController.rebind_hotkey`` + settings_store."""

    def __init__(self, parent: tk.Widget, cfg: "Config", arm: "ArmController") -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._arm = arm

        ttk.Label(
            self._frame, text="Start-recording shortcut",
            style="H1.Sayzo.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self._frame,
            text="Press this anywhere on your computer to start a capture, "
                 "or stop one in progress.",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))

        self._field = ShortcutCaptureField(
            self._frame,
            arm.current_hotkey,
            on_save=self._save,
        )
        self._field.pack(anchor="w", fill="x")

    def _save(self, binding: str) -> Optional[str]:
        err = self._arm.rebind_hotkey(binding)
        if err is not None:
            return err
        try:
            settings_store.save(
                self._cfg.data_dir,
                {"arm": {"hotkey": binding}},
            )
        except Exception:
            log.warning("[settings] persist hotkey failed", exc_info=True)
            return ("Saved to the running agent, but couldn't write to "
                    "user_settings.json.")
        return None


# ---- Permissions pane ------------------------------------------------------


class _PermissionsPane(_Pane):
    """macOS: per-permission rows with Re-request buttons. Windows: a short
    'no permissions required' note."""

    _MAC_ROWS = (
        ("Microphone", "Needed to hear your voice during meetings.", "mic"),
        ("System Audio Recording",
         "Needed to transcribe the other side of the conversation.",
         "audio_capture"),
        ("Accessibility",
         "Lets the global shortcut work when another app is focused.",
         "accessibility"),
        ("Automation (browsers)",
         "Lets Sayzo read the current tab's URL to detect web meetings.",
         "automation"),
    )

    def __init__(self, parent: tk.Widget, cfg: "Config") -> None:
        super().__init__(parent)
        self._cfg = cfg

        ttk.Label(
            self._frame, text="Permissions", style="H1.Sayzo.TLabel",
        ).pack(anchor="w")

        if sys.platform != "darwin":
            ttk.Label(
                self._frame,
                text="Sayzo doesn't need any special permissions on Windows. "
                     "If notifications aren't showing, check Windows "
                     "Settings → System → Notifications.",
                style="Muted.Sayzo.TLabel",
                wraplength=480, justify="left",
            ).pack(anchor="w", pady=(PAD_SM, 0))
            return

        ttk.Label(
            self._frame,
            text="Grant the permissions Sayzo needs to capture meetings and "
                 "let the keyboard shortcut work anywhere.",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))

        for label, desc, key in self._MAC_ROWS:
            self._add_row(label, desc, key)

    def _add_row(self, label: str, desc: str, key: str) -> None:
        row = ttk.Frame(self._frame, style="Sayzo.TFrame")
        row.pack(fill="x", pady=(0, PAD_LG))

        text_col = ttk.Frame(row, style="Sayzo.TFrame")
        text_col.pack(side="left", fill="x", expand=True)
        ttk.Label(text_col, text=label, style="H3.Sayzo.TLabel").pack(anchor="w")
        ttk.Label(
            text_col, text=desc, style="Small.Sayzo.TLabel",
            wraplength=340, justify="left",
        ).pack(anchor="w", pady=(2, 0))

        RoundedButton(
            row, "Re-request",
            command=lambda k=key: self._re_request(k),
            variant="secondary",
        ).pack(side="right", padx=(PAD_MD, 0))

    def _re_request(self, key: str) -> None:
        if sys.platform != "darwin":
            return
        try:
            from .setup import mac_permissions
        except ImportError:
            log.debug("[settings] mac_permissions import failed", exc_info=True)
            return
        try:
            if key == "mic":
                mac_permissions.prompt_microphone()
            elif key == "audio_capture":
                mac_permissions.prompt_audio_capture()
            elif key == "accessibility":
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility",
                ])
            elif key == "automation":
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Automation",
                ])
        except Exception:
            log.warning("[settings] re_request %s failed", key, exc_info=True)


# ---- Account pane ----------------------------------------------------------


class _AccountPane(_Pane):
    """Renders signed-in vs. signed-out state from the TokenStore.

    When signed out, the pane has three UI states to handle the PKCE
    flow's failure modes without dead-ending the user (the tester found
    that a stuck browser flow had no recovery surface before):

    - ``idle``: "You're not signed in" + Sign in button.
    - ``pending``: "Waiting for sign-in…" + countdown + Cancel button +
      "Copy URL" block. This state starts when Sign in is clicked and
      ends on success / cancel / timeout.
    - ``error``: error message + Try again button.
    """

    def __init__(self, parent: tk.Widget, cfg: "Config") -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._inner = ttk.Frame(self._frame, style="Sayzo.TFrame")
        self._inner.pack(fill="both", expand=True)

        # Sign-in state machine (only relevant when signed out).
        self._ui_state: str = "idle"  # "idle" | "pending" | "error"
        self._error_message: Optional[str] = None
        self._login_url: Optional[str] = None
        self._seconds_remaining: Optional[int] = None
        self._cancel_event: Optional[threading.Event] = None
        self._countdown_label: Optional[ttk.Label] = None

        self._render()

    def show(self) -> None:
        # Re-render on each show so state is fresh after sign-in/out.
        # Reset the transient UI state too — someone re-entering the pane
        # after a failed attempt should land on idle, not error.
        self._ui_state = "idle"
        self._error_message = None
        self._login_url = None
        self._seconds_remaining = None
        self._rebuild()
        super().show()

    def _rebuild(self) -> None:
        for child in self._inner.winfo_children():
            child.destroy()
        self._countdown_label = None
        self._render()

    def _has_tokens(self) -> bool:
        from ..auth.store import TokenStore
        try:
            return TokenStore(self._cfg.auth_path).has_tokens()
        except Exception:
            log.debug("[settings] TokenStore read failed", exc_info=True)
            return False

    def _render(self) -> None:
        ttk.Label(
            self._inner, text="Account", style="H1.Sayzo.TLabel",
        ).pack(anchor="w")

        if self._has_tokens():
            self._render_signed_in()
            return

        # Signed-out branch.
        if self._ui_state == "pending":
            self._render_pending()
        elif self._ui_state == "error":
            self._render_error()
        else:
            self._render_idle()

    def _render_idle(self) -> None:
        ttk.Label(
            self._inner,
            text="You're not signed in. Sayzo will keep captures on this "
                 "machine until you do — so no coaching drills yet.",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))
        RoundedButton(
            self._inner, "Sign in",
            command=self._sign_in,
            variant="primary",
        ).pack(anchor="w")

    def _render_pending(self) -> None:
        self._countdown_label = ttk.Label(
            self._inner,
            text=self._pending_text(),
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        )
        self._countdown_label.pack(anchor="w", pady=(PAD_SM, PAD_MD))

        RoundedButton(
            self._inner, "Cancel",
            command=self._cancel_sign_in,
            variant="secondary",
        ).pack(anchor="w", pady=(0, PAD_XL))

        ttk.Label(
            self._inner,
            text="Having trouble? Copy the sign-in URL and paste it into "
                 "any browser to finish.",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(0, PAD_SM))

        url_row = ttk.Frame(self._inner, style="Sayzo.TFrame")
        url_row.pack(anchor="w", fill="x")

        url_var = tk.StringVar(value=self._login_url or "")
        entry = ttk.Entry(url_row, textvariable=url_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=(0, PAD_SM))

        RoundedButton(
            url_row, "Copy",
            command=lambda: self._copy_url_to_clipboard(),
            variant="secondary",
        ).pack(side="left")

    def _render_error(self) -> None:
        ttk.Label(
            self._inner,
            text=f"Sign-in failed: {self._error_message or 'Unknown error.'}",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))
        RoundedButton(
            self._inner, "Try again",
            command=self._sign_in,
            variant="primary",
        ).pack(anchor="w")

    def _render_signed_in(self) -> None:
        ttk.Label(
            self._inner,
            text="Signed in. Your captures sync to your account so you can "
                 "drill the coaching moments in the Sayzo web app.",
            style="Muted.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))

        card = ttk.Frame(self._inner, style="Sayzo.TFrame")
        card.pack(anchor="w", fill="x")

        self._add_kv_row(card, "Server", self._cfg.auth.effective_server_url or "—")
        signed_in_at = self._signed_in_at()
        self._add_kv_row(
            card, "Signed in since",
            signed_in_at.strftime("%b %d, %Y") if signed_in_at else "—",
        )

        make_divider(self._inner, pady=PAD_XL)

        actions = ttk.Frame(self._inner, style="Sayzo.TFrame")
        actions.pack(anchor="w")
        server = self._cfg.auth.effective_server_url
        if server:
            RoundedButton(
                actions, "Open webapp",
                command=lambda s=server: webbrowser.open(s),
                variant="primary",
            ).pack(side="left")
        RoundedButton(
            actions, "Sign out",
            command=self._sign_out,
            variant="danger",
        ).pack(side="left", padx=(PAD_SM, 0))

    def _pending_text(self) -> str:
        if self._seconds_remaining is not None and self._seconds_remaining > 0:
            return (
                f"Waiting for sign-in in your browser… "
                f"({self._seconds_remaining}s left)"
            )
        return "Waiting for sign-in in your browser…"

    def _add_kv_row(self, parent: tk.Widget, key: str, value: str) -> None:
        row = ttk.Frame(parent, style="Sayzo.TFrame")
        row.pack(fill="x", pady=(0, PAD_XS))
        ttk.Label(
            row, text=key, style="Muted.Sayzo.TLabel", width=18,
        ).pack(side="left")
        ttk.Label(
            row, text=value, style="Sayzo.TLabel",
        ).pack(side="left")

    def _signed_in_at(self) -> Optional[datetime]:
        path = self._cfg.auth_path
        if not path.exists():
            return None
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return None

    # ---- sign-in state transitions ---------------------------------------

    def _sign_in(self) -> None:
        self._ui_state = "pending"
        self._error_message = None
        self._login_url = None
        self._seconds_remaining = None
        self._cancel_event = threading.Event()
        self._rebuild()

        cancel_evt = self._cancel_event

        def on_url(url: str) -> None:
            self._frame.after(0, self._on_url_ready, url)

        def on_tick(secs: int) -> None:
            self._frame.after(0, self._on_tick, secs)

        def worker() -> None:
            from ..auth.exceptions import AuthenticationCancelled
            try:
                from ..__main__ import _do_login
                asyncio.run(
                    _do_login(
                        self._cfg,
                        quiet=True,
                        cancel_event=cancel_evt,
                        on_url_ready=on_url,
                        on_tick=on_tick,
                    )
                )
            except AuthenticationCancelled:
                self._frame.after(0, self._on_cancelled)
                return
            except Exception as e:
                log.warning("[settings] login from settings failed", exc_info=True)
                msg = str(e)
                self._frame.after(0, self._on_error, msg)
                return
            self._frame.after(0, self._on_success)

        threading.Thread(
            target=worker, name="settings-login", daemon=True
        ).start()

    def _cancel_sign_in(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        # Worker will emit AuthenticationCancelled → _on_cancelled resets UI.

    def _on_url_ready(self, url: str) -> None:
        self._login_url = url
        if self._ui_state == "pending":
            self._rebuild()

    def _on_tick(self, secs: int) -> None:
        self._seconds_remaining = secs
        if self._ui_state == "pending" and self._countdown_label is not None:
            try:
                self._countdown_label.config(text=self._pending_text())
            except tk.TclError:
                # Widget destroyed (user navigated away) — drop silently.
                pass

    def _on_success(self) -> None:
        self._ui_state = "idle"
        self._cancel_event = None
        self._rebuild()  # re-reads has_tokens → signed-in branch

    def _on_error(self, msg: str) -> None:
        self._ui_state = "error"
        self._error_message = msg
        self._cancel_event = None
        self._rebuild()

    def _on_cancelled(self) -> None:
        self._ui_state = "idle"
        self._cancel_event = None
        self._login_url = None
        self._seconds_remaining = None
        self._rebuild()

    def _copy_url_to_clipboard(self) -> None:
        if not self._login_url:
            return
        try:
            top = self._frame.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(self._login_url)
            # Force an update so the clipboard content sticks after the
            # call returns (tkinter quirk — without update(), the clipboard
            # is cleared when the root's event loop idles).
            top.update()
        except Exception:
            log.debug("[settings] clipboard copy failed", exc_info=True)

    def _sign_out(self) -> None:
        from ..auth.store import TokenStore
        try:
            TokenStore(self._cfg.auth_path).clear()
        except Exception:
            log.warning("[settings] sign-out failed", exc_info=True)
        self.show()  # re-render


# ---- Notifications pane ----------------------------------------------------


class _NotificationsPane(_Pane):
    """Master toggle + three sub-toggles. Mutations apply to the live Config
    and persist to user_settings.json.

    Uses the same ``SwitchToggle`` widget as the Meeting Apps pane so the
    two control-surfaces feel like the same product.
    """

    def __init__(self, parent: tk.Widget, cfg: "Config") -> None:
        super().__init__(parent)
        self._cfg = cfg

        self._master_switch: Optional[SwitchToggle] = None
        self._welcome_switch: Optional[SwitchToggle] = None
        self._post_arm_switch: Optional[SwitchToggle] = None
        self._saved_switch: Optional[SwitchToggle] = None

        ttk.Label(
            self._frame, text="Notifications", style="H1.Sayzo.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self._frame,
            text="Choose which Sayzo toasts show up on your desktop.",
            style="Muted.Sayzo.TLabel",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))

        self._master_switch = self._add_row(
            self._frame,
            label="Show Sayzo notifications",
            on=cfg.notifications_enabled,
            indent=False,
        )

        sub = ttk.Frame(self._frame, style="Sayzo.TFrame")
        sub.pack(fill="x", pady=(PAD_SM, PAD_LG))
        self._welcome_switch = self._add_row(
            sub,
            label="Show the welcome message on first launch",
            on=cfg.notify_welcome,
            indent=True,
        )
        self._post_arm_switch = self._add_row(
            sub,
            label="Show “Sayzo is capturing” reminders after I arm",
            on=cfg.arm.notify_post_arm,
            indent=True,
        )
        self._saved_switch = self._add_row(
            sub,
            label="Show “Conversation saved” when a capture finishes",
            on=cfg.notify_capture_saved,
            indent=True,
        )

        ttk.Label(
            self._frame,
            text="Consent prompts and end-of-meeting questions always show — "
                 "they're how you decide what Sayzo captures.",
            style="Small.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_MD, 0))

    def _add_row(
        self, parent: tk.Widget, *, label: str, on: bool, indent: bool,
    ) -> SwitchToggle:
        """Add a single [label ........ switch] row and return the switch
        so the caller can read its state on persist."""
        row = tk.Frame(parent, background=BG)
        row.pack(
            fill="x",
            pady=PAD_XS,
            padx=(PAD_XL if indent else 0, 0),
        )
        tk.Label(
            row, text=label,
            background=BG, foreground=INK, font=theme.FONT_BODY,
            anchor="w", justify="left", wraplength=480,
        ).pack(side="left", fill="x", expand=True)
        switch = SwitchToggle(
            row, on=on, command=lambda _v: self._persist(),
        )
        switch.pack(side="right")
        return switch

    def _persist(self) -> None:
        assert self._master_switch is not None
        assert self._welcome_switch is not None
        assert self._post_arm_switch is not None
        assert self._saved_switch is not None
        master = self._master_switch.on
        welcome = self._welcome_switch.on
        post_arm = self._post_arm_switch.on
        saved = self._saved_switch.on

        self._cfg.notifications_enabled = master
        self._cfg.notify_welcome = welcome
        self._cfg.arm.notify_post_arm = post_arm
        self._cfg.notify_capture_saved = saved

        try:
            settings_store.save(
                self._cfg.data_dir,
                {
                    "notifications_enabled": master,
                    "notify_welcome": welcome,
                    "notify_capture_saved": saved,
                    "arm": {"notify_post_arm": post_arm},
                },
            )
        except Exception:
            log.warning("[settings] persist notifications failed", exc_info=True)


# ---- Meeting Apps pane -----------------------------------------------------


class _MeetingAppsPane(_Pane):
    """Whitelist editor. Shows every detector as a row with toggle +
    remove; offers an Add dialog for desktop apps (live mic-holder picker)
    and web meetings (URL paste). A Suggested section surfaces unmatched
    mic-holders Sayzo has observed while disarmed.

    Persistence: the pane writes the full detector list to
    ``user_settings.json`` under ``arm.detectors``. ``load_config`` merges
    that onto the defaults, so any env var (``SAYZO_ARM__DETECTORS``) still
    wins. "Reset to defaults" clears the user's override so the ship-with
    list reappears.
    """

    _UNDO_TIMEOUT_MS = 8000

    def __init__(
        self, parent: tk.Widget, cfg: "Config", arm: "ArmController",
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._arm = arm

        # Undo state — used by Reset to defaults so an accidental reset
        # is recoverable. Toggle is self-undoing (click again) so it
        # doesn't need the snapshot.
        self._undo_snapshot: Optional[list[DetectorSpec]] = None
        self._undo_label: str = ""
        self._undo_after_id: Optional[str] = None

        # Which section is currently visible — "desktop" or "web". Tabbed
        # UI so the user sees one section at a time rather than scrolling
        # through both lists + their identical display names.
        self._active_section: str = "desktop"
        self._tab_desktop_btn: Optional[RoundedButton] = None
        self._tab_web_btn: Optional[RoundedButton] = None
        self._tabs_row: Optional[tk.Frame] = None

        # Add-app button — rebuilt on section switch so its label reflects
        # the active tab ("+ Add desktop app" vs "+ Add web meeting"). The
        # dialog also opens on the matching tab, so the whole control
        # group reads as one contextual action.
        self._actions_row: Optional[tk.Frame] = None

        # Scrollable list state.
        self._list_wrap: Optional[ttk.Frame] = None
        self._list_canvas: Optional[tk.Canvas] = None
        self._list_inner: Optional[tk.Frame] = None
        self._list_window_id: Optional[int] = None

        # Undo bar ref — kept so show/hide just toggles pack() without
        # rebuilding.
        self._undo_bar: Optional[tk.Frame] = None
        self._undo_text_var = tk.StringVar(value="")

        self._build_structure()
        self._render_list()

    # ---- lifecycle ---------------------------------------------------------

    def show(self) -> None:
        super().show()
        # Refresh on show so a detector added via env var or an updated
        # seen_apps file doesn't show stale.
        self._render_list()

    def hide(self) -> None:
        # Cancel any pending undo timer so it doesn't fire on a hidden pane.
        self._cancel_undo_timer()
        self._undo_snapshot = None
        self._set_undo_bar_visible(False)
        super().hide()

    # ---- one-time structural build ----------------------------------------

    def _build_structure(self) -> None:
        """Static elements that never rebuild: header, action bar, undo
        bar (hidden initially), and the scrollable list shell. The list's
        *contents* re-render on every change via :meth:`_render_list`."""
        header = ttk.Frame(self._frame, style="Sayzo.TFrame")
        header.pack(fill="x")
        ttk.Label(
            header, text="Meeting Apps", style="H1.Sayzo.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="Sayzo asks to start coaching when one of these apps is in "
                 "a meeting. Toggle an app off to stop matching it without "
                 "losing its settings.",
            style="Muted.Sayzo.TLabel",
            wraplength=580, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, PAD_LG))

        # Section tabs: [Desktop apps] [Web meetings]. These set the
        # context for the action bar below, so they come first. Same
        # segmented-button pattern as the Add-app dialog so the two
        # surfaces feel like the same product.
        self._tabs_row = tk.Frame(self._frame, background=BG)
        self._tabs_row.pack(fill="x", pady=(0, PAD_SM))
        self._render_tab_buttons()

        # Action bar: [+ Add <section>]    [Reset to defaults]. The Add
        # button's label + target tab tracks the active section so it's
        # obvious what you're about to add.
        self._actions_row = tk.Frame(self._frame, background=BG)
        self._actions_row.pack(fill="x", pady=(0, PAD_MD))
        self._render_actions_row()

        # Undo bar (hidden until an undoable action fires).
        self._undo_bar = tk.Frame(
            self._frame,
            background=ACCENT_TINT,
            highlightthickness=1,
            highlightbackground=ACCENT,
        )
        undo_inner = tk.Frame(self._undo_bar, background=ACCENT_TINT)
        undo_inner.pack(fill="x", padx=PAD_MD, pady=PAD_SM)
        tk.Label(
            undo_inner,
            textvariable=self._undo_text_var,
            background=ACCENT_TINT,
            foreground=INK,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        RoundedButton(
            undo_inner, "Undo",
            command=self._on_undo,
            variant="secondary",
            bg=ACCENT_TINT,
        ).pack(side="right")

        # Scrollable list area. Canvas + inner frame so ttk buttons can
        # sit inside (Canvas.create_window does the windowed-widget trick).
        list_wrap = ttk.Frame(self._frame, style="Sayzo.TFrame")
        list_wrap.pack(fill="both", expand=True)
        self._list_wrap = list_wrap

        canvas = tk.Canvas(
            list_wrap, background=BG, highlightthickness=0, bd=0,
        )
        scrollbar = ttk.Scrollbar(
            list_wrap, orient="vertical", command=canvas.yview,
            style="Sayzo.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(PAD_XS, 0))

        inner = tk.Frame(canvas, background=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e: tk.Event) -> None:
            canvas.itemconfigure(window_id, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel only while hovering the list, to avoid swallowing
        # scroll events from other widgets.
        def _on_wheel(e: tk.Event) -> None:
            # Windows: e.delta is ±120 per notch. macOS: smaller, signed.
            try:
                canvas.yview_scroll(-int(e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
            except tk.TclError:
                pass

        def _bind_wheel(_e: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", _on_wheel)

        def _unbind_wheel(_e: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        self._list_canvas = canvas
        self._list_inner = inner
        self._list_window_id = window_id

    # ---- list re-rendering -------------------------------------------------

    def _render_list(self) -> None:
        """Rebuild the rows for the active section + the Suggested list.

        Called on first build, on every list mutation (toggle / add / reset),
        on pane show(), and on section tab switch. Cheap enough to do
        wholesale — at ~15 rows per section + one frame recreate, render
        time is <10 ms on any machine that can run the agent.
        """
        if self._list_inner is None:
            return
        for child in self._list_inner.winfo_children():
            child.destroy()

        detectors = list(self._cfg.arm.detectors)
        if self._active_section == "web":
            visible = [s for s in detectors if s.is_browser]
            empty_hint = (
                "No web meetings on your list. Click "
                "“+ Add app” above to add one from a URL."
            )
        else:
            visible = [s for s in detectors if not s.is_browser]
            empty_hint = (
                "No desktop apps on your list. Click "
                "“+ Add app” above to add one — or start a "
                "meeting and Sayzo will suggest it automatically."
            )

        if not visible:
            tk.Label(
                self._list_inner, text=empty_hint,
                background=BG, foreground=MUTED, font=theme.FONT_BODY,
                anchor="w", justify="left", wraplength=560,
            ).pack(fill="x", pady=(PAD_SM, 0))
        else:
            container = tk.Frame(self._list_inner, background=BG)
            container.pack(fill="x")
            for i, spec in enumerate(visible):
                if i > 0:
                    tk.Frame(container, background=BORDER, height=1).pack(fill="x")
                self._build_detector_row(container, spec)

        # Suggested section — only on the Desktop tab (the watcher skips
        # browsers when recording seen apps, so every suggestion is a
        # desktop app).
        if self._active_section == "desktop":
            suggested = _seen_apps.load(self._cfg.data_dir, detectors)
            if suggested:
                make_divider(self._list_inner, pady=PAD_LG)
                tk.Label(
                    self._list_inner,
                    text="Suggested to add",
                    background=BG, foreground=MUTED, font=theme.FONT_STEP,
                    anchor="w",
                ).pack(fill="x", pady=(0, PAD_XS))
                tk.Label(
                    self._list_inner,
                    text="Apps Sayzo saw using your microphone that aren't "
                         "on your list yet.",
                    background=BG, foreground=MUTED, font=theme.FONT_SMALL,
                    anchor="w", wraplength=560, justify="left",
                ).pack(fill="x", pady=(0, PAD_SM))
                for seen in suggested[:5]:  # cap UI to top-5 most recent
                    self._build_suggested_row(self._list_inner, seen)

        # Tail padding so the last row isn't flush against the scrollbar.
        tk.Frame(self._list_inner, background=BG, height=PAD_XL).pack(fill="x")

    def _build_detector_row(self, parent: tk.Widget, spec: DetectorSpec) -> None:
        """One detector row: name + detail on the left, switch on the right.

        There used to be a Remove button here too — it's been dropped because
        the toggle already does the job ("stop matching this app") and
        having both forced a cognitive decision per row with no real payoff.
        Clearing the list entirely is handled by Reset to defaults, and
        accidentally-added custom entries can just be toggled off.
        """
        row = tk.Frame(parent, background=BG)
        row.pack(fill="x")

        body = tk.Frame(row, background=BG)
        body.pack(fill="x", pady=PAD_SM)

        # Text column (left, takes all available space).
        text_col = tk.Frame(body, background=BG)
        text_col.pack(side="left", fill="x", expand=True)

        name_color = INK if not spec.disabled else SUBTLE
        tk.Label(
            text_col,
            text=spec.display_name,
            background=BG, foreground=name_color,
            font=theme.FONT_BODY_BOLD, anchor="w",
        ).pack(anchor="w")

        detail = self._detail_text(spec)
        if detail:
            detail_color = MUTED if not spec.disabled else SUBTLE
            tk.Label(
                text_col,
                text=detail,
                background=BG, foreground=detail_color, font=theme.FONT_SMALL,
                anchor="w", justify="left", wraplength=460,
            ).pack(anchor="w", pady=(1, 0))

        SwitchToggle(
            body,
            on=not spec.disabled,
            command=lambda enabled, s=spec: self._on_toggle_switch(s, enabled),
        ).pack(side="right", padx=(PAD_MD, 0))

    def _build_suggested_row(self, parent: tk.Widget, seen: "_seen_apps.SeenApp") -> None:
        row = tk.Frame(parent, background=BG)
        row.pack(fill="x", pady=(PAD_XS, 0))

        body = tk.Frame(row, background=BG)
        body.pack(fill="x", pady=PAD_XS)

        text_col = tk.Frame(body, background=BG)
        text_col.pack(side="left", fill="x", expand=True)
        tk.Label(
            text_col,
            text=seen.display_name,
            background=BG, foreground=INK, font=theme.FONT_BODY_BOLD,
            anchor="w",
        ).pack(anchor="w")
        detail_key = seen.process_name or seen.bundle_id or seen.key
        tk.Label(
            text_col,
            text=detail_key,
            background=BG, foreground=MUTED, font=theme.FONT_SMALL, anchor="w",
        ).pack(anchor="w", pady=(1, 0))

        RoundedButton(
            body, "Dismiss",
            command=lambda s=seen: self._on_dismiss_suggested(s),
            variant="ghost",
            padx=PAD_MD, pady=PAD_XS,
        ).pack(side="right")
        RoundedButton(
            body, "+ Add",
            command=lambda s=seen: self._on_add_suggested(s),
            variant="secondary",
            padx=PAD_MD, pady=PAD_XS,
        ).pack(side="right", padx=(0, PAD_SM))

    @staticmethod
    def _detail_text(spec: DetectorSpec) -> str:
        if spec.is_browser:
            # For web specs, show the hostname distilled from the first URL
            # pattern. Regex-y strings aren't friendly — strip the common
            # regex glyphs so "^https://meet\\.google\\.com/..." reads as
            # "meet.google.com/…".
            if spec.url_patterns:
                return "Web · " + _friendly_url_pattern(spec.url_patterns[0])
            if spec.title_patterns:
                return "Web · matches window titles"
            return "Web"
        bits: list[str] = []
        if spec.process_names:
            bits.append(", ".join(spec.process_names))
        if spec.bundle_ids and not spec.process_names:
            bits.append(", ".join(spec.bundle_ids))
        return ("Desktop · " + bits[0]) if bits else "Desktop"

    # ---- section tabs -----------------------------------------------------

    def _render_tab_buttons(self) -> None:
        """Build / rebuild the segmented [Desktop apps] [Web meetings]
        toggle. Called on initial build + on every section switch (the
        RoundedButton variant is picked at construction time, so we
        destroy + recreate on flip)."""
        if self._tabs_row is None:
            return
        for child in self._tabs_row.winfo_children():
            child.destroy()
        desktop_selected = self._active_section == "desktop"
        self._tab_desktop_btn = RoundedButton(
            self._tabs_row, "Desktop apps",
            command=lambda: self._switch_section("desktop"),
            variant="primary" if desktop_selected else "secondary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_desktop_btn.pack(side="left")
        self._tab_web_btn = RoundedButton(
            self._tabs_row, "Web meetings",
            command=lambda: self._switch_section("web"),
            variant="secondary" if desktop_selected else "primary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_web_btn.pack(side="left", padx=(PAD_SM, 0))

    def _switch_section(self, section: str) -> None:
        if section == self._active_section:
            return
        self._active_section = section
        self._render_tab_buttons()
        self._render_actions_row()
        self._render_list()

    def _render_actions_row(self) -> None:
        """Rebuild the [+ Add <section>] [Reset to defaults] bar.

        The Add button's label reflects the active section so that reading
        left-to-right ("Desktop apps" tab → "+ Add desktop app") telegraphs
        exactly what the button does.
        """
        if self._actions_row is None:
            return
        for child in self._actions_row.winfo_children():
            child.destroy()
        label = (
            "+ Add desktop app"
            if self._active_section == "desktop"
            else "+ Add web meeting"
        )
        RoundedButton(
            self._actions_row, label,
            command=self._on_add_clicked,
            variant="primary",
        ).pack(side="left")
        RoundedButton(
            self._actions_row, "Reset to defaults",
            command=self._on_reset,
            variant="ghost",
        ).pack(side="right")

    # ---- mutation handlers ------------------------------------------------

    def _on_toggle_switch(self, spec: DetectorSpec, enabled: bool) -> None:
        spec.disabled = not bool(enabled)
        self._persist()
        # Re-render to update the label color + detail muting. The switch
        # widget already shows the new state; the re-render is purely for
        # the text fade.
        self._render_list()

    def _on_undo(self) -> None:
        if self._undo_snapshot is None:
            return
        self._cfg.arm.detectors = list(self._undo_snapshot)
        self._persist()
        self._cancel_undo_timer()
        self._undo_snapshot = None
        self._set_undo_bar_visible(False)
        self._render_list()

    def _on_reset(self) -> None:
        self._snapshot_for_undo("Reset to the built-in app list.")
        # Reset clears the user override — we overwrite with the defaults
        # and also clear the user_settings entry so ``load_config`` picks
        # up any future shipped defaults on next launch.
        self._cfg.arm.detectors = default_detector_specs()
        self._persist(clear_user_override=True)
        self._render_list()

    def _on_add_clicked(self) -> None:
        dialog = _AddAppDialog(
            self._frame.winfo_toplevel(),
            cfg=self._cfg,
            arm=self._arm,
            existing=list(self._cfg.arm.detectors),
            initial_tab=self._active_section,
        )
        spec = dialog.run()
        if spec is None:
            return
        self._add_detector(spec)

    def _on_add_suggested(self, seen: "_seen_apps.SeenApp") -> None:
        """Convert a suggested entry into a DetectorSpec and add it."""
        spec = DetectorSpec(
            app_key=_unique_app_key(seen.key, [d.app_key for d in self._cfg.arm.detectors]),
            display_name=seen.display_name or seen.key,
            process_names=[seen.process_name] if seen.process_name else [],
            bundle_ids=[seen.bundle_id] if seen.bundle_id else [],
        )
        self._add_detector(spec)
        # Drop the suggestion entry so it doesn't linger after we've
        # incorporated it.
        try:
            _seen_apps.dismiss(self._cfg.data_dir, seen.key)
        except Exception:
            log.debug("[settings] seen_apps.dismiss failed", exc_info=True)

    def _on_dismiss_suggested(self, seen: "_seen_apps.SeenApp") -> None:
        try:
            _seen_apps.dismiss(self._cfg.data_dir, seen.key)
        except Exception:
            log.warning("[settings] seen_apps.dismiss failed", exc_info=True)
        self._render_list()

    def _add_detector(self, spec: DetectorSpec) -> None:
        # If a detector with this app_key already exists, replace it (the
        # Add dialog guards against this too, but this is defensive).
        existing = [d for d in self._cfg.arm.detectors if d.app_key != spec.app_key]
        existing.append(spec)
        self._cfg.arm.detectors = existing
        self._persist()
        # Clear any prior dismissal for this app's keys so future
        # observations accumulate normally if the user later disables /
        # replaces the detector.
        for k in (*spec.process_names, *spec.bundle_ids):
            try:
                _seen_apps.undismiss(self._cfg.data_dir, k)
            except Exception:
                log.debug("[settings] undismiss failed", exc_info=True)
        self._render_list()

    # ---- persistence / undo timer -----------------------------------------

    def _persist(self, *, clear_user_override: bool = False) -> None:
        """Write the current detector list to ``user_settings.json``.

        ``clear_user_override=True`` drops the whole ``arm.detectors`` key
        from the JSON so ``load_config`` falls back to ``default_detector_specs``
        on next launch. In-memory ``cfg.arm.detectors`` is kept in sync
        either way.
        """
        try:
            if clear_user_override:
                # Read-modify-write: load current JSON, drop the key, rewrite.
                current = settings_store.load(self._cfg.data_dir)
                arm_block = current.get("arm") if isinstance(current.get("arm"), dict) else {}
                if isinstance(arm_block, dict) and "detectors" in arm_block:
                    arm_block.pop("detectors", None)
                    settings_store.save(
                        self._cfg.data_dir, {"arm": arm_block},
                    )
                return
            serialized = [d.model_dump() for d in self._cfg.arm.detectors]
            settings_store.save(
                self._cfg.data_dir,
                {"arm": {"detectors": serialized}},
            )
        except Exception:
            log.warning("[settings] persist detectors failed", exc_info=True)

    def _snapshot_for_undo(self, label: str) -> None:
        self._cancel_undo_timer()
        # Deep-copy via model_dump/model_validate so mutating cfg.arm.detectors
        # later doesn't retroactively alter the snapshot.
        self._undo_snapshot = [
            DetectorSpec.model_validate(d.model_dump())
            for d in self._cfg.arm.detectors
        ]
        self._undo_label = label
        self._undo_text_var.set(label)
        self._set_undo_bar_visible(True)
        try:
            self._undo_after_id = self._frame.after(
                self._UNDO_TIMEOUT_MS, self._expire_undo,
            )
        except tk.TclError:
            self._undo_after_id = None

    def _expire_undo(self) -> None:
        self._undo_after_id = None
        self._undo_snapshot = None
        self._set_undo_bar_visible(False)

    def _cancel_undo_timer(self) -> None:
        if self._undo_after_id is None:
            return
        try:
            self._frame.after_cancel(self._undo_after_id)
        except tk.TclError:
            pass
        self._undo_after_id = None

    def _set_undo_bar_visible(self, visible: bool) -> None:
        if self._undo_bar is None:
            return
        if visible:
            # Slot the undo bar between the action row and the list. Using
            # `before=list_wrap` keeps the visual order stable.
            try:
                if self._list_wrap is not None:
                    self._undo_bar.pack(
                        fill="x", pady=(0, PAD_MD), before=self._list_wrap,
                    )
                else:
                    self._undo_bar.pack(fill="x", pady=(0, PAD_MD))
            except tk.TclError:
                pass
        else:
            try:
                self._undo_bar.pack_forget()
            except tk.TclError:
                pass


# ---- Add-app dialog -------------------------------------------------------


class _AddAppDialog:
    """Modal dialog for adding a new detector. Two tabs — Desktop and Web —
    and returns a ``DetectorSpec`` (or None on cancel). Runs its own
    ``wait_window`` so the parent Settings pane blocks until the dialog
    closes and only then incorporates the result.
    """

    _REFRESH_INTERVAL_MS = 2000
    _DIALOG_SIZE = (720, 640)
    _DIALOG_MIN = (640, 560)

    def __init__(
        self,
        parent: tk.Misc,
        *,
        cfg: "Config",
        arm: "ArmController",
        existing: list[DetectorSpec],
        initial_tab: str = "desktop",
    ) -> None:
        self._cfg = cfg
        self._arm = arm
        self._existing = existing
        self._result: Optional[DetectorSpec] = None
        # ``initial_tab`` lets callers open the dialog pre-focused on the
        # tab they're adding from (the Meeting Apps pane passes the
        # active section), so the user doesn't have to re-pick.
        self._active_tab = initial_tab if initial_tab in ("desktop", "web") else "desktop"
        self._refresh_after_id: Optional[str] = None

        self._top = tk.Toplevel(parent)
        self._top.title("Add a meeting app")
        self._top.geometry(f"{self._DIALOG_SIZE[0]}x{self._DIALOG_SIZE[1]}")
        self._top.minsize(*self._DIALOG_MIN)
        self._top.configure(background=BG)
        apply_sayzo_icon(self._top)
        # Modal-ish: transient + grab so the user can't interact with the
        # underlying Settings window while adding.
        try:
            self._top.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass
        self._top.grab_set()

        self._desktop_tab: Optional[tk.Frame] = None
        self._web_tab: Optional[tk.Frame] = None
        self._tab_desktop_btn: Optional[RoundedButton] = None
        self._tab_web_btn: Optional[RoundedButton] = None

        # Desktop state.
        self._desktop_list_frame: Optional[tk.Frame] = None
        self._desktop_empty_label: Optional[tk.Label] = None
        self._manual_name_var = tk.StringVar()
        self._manual_process_var = tk.StringVar()
        self._manual_bundle_var = tk.StringVar()
        self._manual_expanded = False
        self._manual_body: Optional[tk.Frame] = None
        self._manual_toggle_btn: Optional[RoundedButton] = None
        self._desktop_status_var = tk.StringVar(value="")

        # Web state.
        self._web_url_var = tk.StringVar()
        self._web_name_var = tk.StringVar()
        self._web_strict_var = tk.BooleanVar(value=False)
        self._web_preview_var = tk.StringVar(value="")
        self._web_status_var = tk.StringVar(value="")

        self._build()
        # Kick off the desktop live-refresh loop.
        self._schedule_desktop_refresh(immediate=True)

        # Cancel timer + grab release on close — even if the user hits the
        # window [X].
        self._top.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def run(self) -> Optional[DetectorSpec]:
        self._top.wait_window()
        return self._result

    # ---- layout ----------------------------------------------------------

    def _build(self) -> None:
        outer = tk.Frame(self._top, background=BG)
        outer.pack(fill="both", expand=True, padx=PAD_XL, pady=PAD_XL)

        tk.Label(
            outer, text="Add a meeting app",
            background=BG, foreground=INK, font=theme.FONT_H1,
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="Tell Sayzo which apps or sites count as meetings, so it "
                 "can offer to capture them.",
            background=BG, foreground=MUTED, font=theme.FONT_BODY,
            anchor="w", justify="left", wraplength=560,
        ).pack(anchor="w", pady=(PAD_XS, PAD_LG))

        # Segmented tab control — two buttons, one primary, one secondary.
        # Initial variants honour ``_active_tab`` so the caller's preference
        # lights up the right tab from the first paint.
        tabs = tk.Frame(outer, background=BG)
        tabs.pack(anchor="w", pady=(0, PAD_LG))
        desktop_selected = self._active_tab == "desktop"
        self._tab_desktop_btn = RoundedButton(
            tabs, "Desktop app",
            command=lambda: self._switch_tab("desktop"),
            variant="primary" if desktop_selected else "secondary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_desktop_btn.pack(side="left")
        self._tab_web_btn = RoundedButton(
            tabs, "Web meeting",
            command=lambda: self._switch_tab("web"),
            variant="secondary" if desktop_selected else "primary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_web_btn.pack(side="left", padx=(PAD_SM, 0))

        # Tab bodies. Each tab is a scrollable canvas so the Web tab's
        # Display-name field (and the Desktop tab's manual-entry expander)
        # never fall off the bottom on a smaller window. Only one tab is
        # packed into the outer frame at a time.
        self._desktop_tab, desktop_inner = self._make_scrollable_tab(outer)
        self._web_tab, web_inner = self._make_scrollable_tab(outer)
        self._build_desktop_tab(desktop_inner)
        self._build_web_tab(web_inner)
        if desktop_selected:
            self._desktop_tab.pack(fill="both", expand=True)
        else:
            self._web_tab.pack(fill="both", expand=True)

        # Footer: Cancel / Add.
        make_divider(outer, pady=PAD_LG)
        footer = tk.Frame(outer, background=BG)
        footer.pack(fill="x")
        RoundedButton(
            footer, "Cancel",
            command=self._on_cancel,
            variant="ghost",
        ).pack(side="right", padx=(PAD_SM, 0))
        RoundedButton(
            footer, "Add app",
            command=self._on_submit,
            variant="primary",
        ).pack(side="right")

    def _make_scrollable_tab(
        self, parent: tk.Widget,
    ) -> tuple[tk.Frame, tk.Frame]:
        """Wrap tab content in a vertical-scroll canvas.

        Returns ``(outer, inner)`` — ``outer`` is what the caller packs /
        unpacks when switching tabs, ``inner`` is where the tab content
        actually lives.
        """
        outer = tk.Frame(parent, background=BG)
        canvas = tk.Canvas(outer, background=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            outer, orient="vertical", command=canvas.yview,
            style="Sayzo.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(PAD_XS, 0))

        inner = tk.Frame(canvas, background=BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e: tk.Event) -> None:
            canvas.itemconfigure(win_id, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(e: tk.Event) -> None:
            try:
                canvas.yview_scroll(
                    -int(e.delta / 120) or (-1 if e.delta > 0 else 1),
                    "units",
                )
            except tk.TclError:
                pass

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return outer, inner

    # ---- Desktop tab ----------------------------------------------------

    def _build_desktop_tab(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text="Pick an app that's using your microphone",
            background=BG, foreground=INK, font=theme.FONT_H3, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            parent,
            text="Open (or join) your meeting, then click the app below to "
                 "add it. Sayzo reads the apps currently recording from your "
                 "microphone — nothing is sent anywhere.",
            background=BG, foreground=MUTED, font=theme.FONT_SMALL,
            anchor="w", justify="left", wraplength=560,
        ).pack(anchor="w", pady=(PAD_XS, PAD_SM))

        # Live list container.
        self._desktop_list_frame = tk.Frame(parent, background=BG)
        self._desktop_list_frame.pack(fill="x")

        # Refresh row.
        refresh_row = tk.Frame(parent, background=BG)
        refresh_row.pack(fill="x", pady=(PAD_SM, 0))
        RoundedButton(
            refresh_row, "Refresh now",
            command=lambda: self._schedule_desktop_refresh(immediate=True),
            variant="secondary",
            padx=PAD_MD, pady=PAD_XS,
        ).pack(side="left")
        tk.Label(
            refresh_row,
            textvariable=self._desktop_status_var,
            background=BG, foreground=SUCCESS, font=theme.FONT_SMALL,
        ).pack(side="left", padx=(PAD_SM, 0))

        make_divider(parent, pady=PAD_LG)

        # Advanced / manual entry.
        self._manual_toggle_btn = RoundedButton(
            parent, "▸ Know the app? Add it by name instead",
            command=self._toggle_manual,
            variant="ghost",
            padx=0, pady=PAD_XS,
        )
        self._manual_toggle_btn.pack(anchor="w")

        self._manual_body = tk.Frame(parent, background=BG)
        # (Not packed yet — toggled by _toggle_manual.)
        self._build_manual_body(self._manual_body)

    def _build_manual_body(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text="Name it",
            background=BG, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XS))
        tk.Label(
            parent,
            text="How this appears in your Meeting Apps list "
                 "(e.g. “Zoom”, “Team standup”).",
            background=BG, foreground=MUTED, font=theme.FONT_SMALL,
            anchor="w", justify="left", wraplength=520,
        ).pack(anchor="w", pady=(0, PAD_XS))
        ttk.Entry(
            parent, textvariable=self._manual_name_var,
        ).pack(fill="x", pady=(0, PAD_SM))

        if sys.platform == "darwin":
            tk.Label(
                parent,
                text="Bundle identifier (e.g. com.hnc.Discord)",
                background=BG, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
            ).pack(anchor="w", pady=(0, PAD_XS))
            ttk.Entry(
                parent, textvariable=self._manual_bundle_var,
            ).pack(fill="x", pady=(0, PAD_XS))
            tk.Label(
                parent,
                text="Find it in macOS: open the app, then Apple menu → "
                     "System Information → Applications. The bundle id is "
                     "in the details panel.",
                background=BG, foreground=MUTED, font=theme.FONT_SMALL,
                wraplength=520, justify="left", anchor="w",
            ).pack(anchor="w", pady=(0, PAD_SM))
        else:
            tk.Label(
                parent,
                text="Process name (e.g. loom.exe)",
                background=BG, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
            ).pack(anchor="w", pady=(0, PAD_XS))
            ttk.Entry(
                parent, textvariable=self._manual_process_var,
            ).pack(fill="x", pady=(0, PAD_XS))
            tk.Label(
                parent,
                text="Find it in Task Manager → Details tab. The process "
                     "name ends in .exe — use the exact filename.",
                background=BG, foreground=MUTED, font=theme.FONT_SMALL,
                wraplength=520, justify="left", anchor="w",
            ).pack(anchor="w", pady=(0, PAD_SM))

    def _toggle_manual(self) -> None:
        self._manual_expanded = not self._manual_expanded
        assert self._manual_toggle_btn is not None
        assert self._manual_body is not None
        if self._manual_expanded:
            self._manual_toggle_btn.set_text("▾ Know the app? Add it by name instead")
            self._manual_body.pack(fill="x", anchor="w")
        else:
            self._manual_toggle_btn.set_text("▸ Know the app? Add it by name instead")
            self._manual_body.pack_forget()

    # ---- Desktop live refresh -----------------------------------------

    def _schedule_desktop_refresh(self, *, immediate: bool = False) -> None:
        """Fetch the current mic-holder snapshot + re-render the list.

        Runs every :attr:`_REFRESH_INTERVAL_MS` while the dialog is open,
        independent of the main whitelist watcher's cadence. Skips cleanly
        if the dialog was closed between schedules.
        """
        if not self._top.winfo_exists():
            return
        if immediate:
            # Cancel pending + run now.
            self._cancel_desktop_refresh()
            self._render_desktop_list()
            self._refresh_after_id = self._top.after(
                self._REFRESH_INTERVAL_MS, self._schedule_desktop_refresh,
            )
            return
        self._render_desktop_list()
        self._refresh_after_id = self._top.after(
            self._REFRESH_INTERVAL_MS, self._schedule_desktop_refresh,
        )

    def _cancel_desktop_refresh(self) -> None:
        if self._refresh_after_id is None:
            return
        try:
            self._top.after_cancel(self._refresh_after_id)
        except tk.TclError:
            pass
        self._refresh_after_id = None

    def _render_desktop_list(self) -> None:
        if self._desktop_list_frame is None:
            return
        for child in self._desktop_list_frame.winfo_children():
            child.destroy()

        try:
            mic = self._arm.snapshot_mic_state()
            fg = self._arm.snapshot_foreground()
        except Exception:
            log.debug("[settings] snapshot failed", exc_info=True)
            mic = None
            fg = None

        # Deduped candidates this snapshot.
        candidates: list[tuple[str, str, bool]] = []  # (key, display, is_bundle_id)
        seen_keys: set[str] = set()

        if mic is not None:
            for holder in mic.holders:
                key = (holder.process_name or "").lower()
                if not key or key in seen_keys:
                    continue
                if key in BROWSER_PROCESS_NAMES:
                    continue
                if self._key_already_present(key):
                    continue
                seen_keys.add(key)
                display = _seen_apps._display_name_for_process(holder.process_name)
                candidates.append((holder.process_name, display, False))

            # macOS fallback: foreground bundle id while mic is active.
            if sys.platform == "darwin" and mic.active and fg is not None and fg.bundle_id:
                if not fg.is_browser:
                    key = fg.bundle_id.lower()
                    if key not in seen_keys and not self._key_already_present(key):
                        display = _seen_apps._display_name_for_bundle(fg.bundle_id)
                        candidates.append((fg.bundle_id, display, True))
                        seen_keys.add(key)

        if not candidates:
            # Helpful empty state.
            self._desktop_empty_label = tk.Label(
                self._desktop_list_frame,
                text="No apps are using your microphone right now.\n"
                     "Start a call in the app you want to add — it will appear here.",
                background=BG, foreground=MUTED, font=theme.FONT_BODY,
                justify="left", anchor="w", wraplength=560,
            )
            self._desktop_empty_label.pack(anchor="w", pady=(PAD_SM, 0))
            return

        for raw_key, display, is_bundle_id in candidates:
            row = tk.Frame(self._desktop_list_frame, background=SURFACE)
            row.pack(fill="x", pady=(0, PAD_XS))

            body = tk.Frame(row, background=SURFACE)
            body.pack(fill="x", padx=PAD_MD, pady=PAD_SM)

            text_col = tk.Frame(body, background=SURFACE)
            text_col.pack(side="left", fill="x", expand=True)
            tk.Label(
                text_col, text=display,
                background=SURFACE, foreground=INK, font=theme.FONT_BODY_BOLD,
                anchor="w",
            ).pack(anchor="w")
            tk.Label(
                text_col, text=raw_key,
                background=SURFACE, foreground=MUTED, font=theme.FONT_SMALL,
                anchor="w",
            ).pack(anchor="w", pady=(1, 0))

            RoundedButton(
                body, "+ Add",
                command=lambda r=raw_key, d=display, b=is_bundle_id: self._submit_desktop_pick(r, d, b),
                variant="secondary",
                padx=PAD_MD, pady=PAD_XS,
                bg=SURFACE,
            ).pack(side="right")

    def _key_already_present(self, key_lc: str) -> bool:
        for spec in self._existing:
            for p in spec.process_names:
                if p.lower() == key_lc:
                    return True
            for b in spec.bundle_ids:
                if b.lower() == key_lc:
                    return True
        return False

    def _submit_desktop_pick(
        self, raw_key: str, display: str, is_bundle_id: bool,
    ) -> None:
        """A one-click add from the live list — build a spec + return."""
        spec = DetectorSpec(
            app_key=_unique_app_key(
                raw_key, [d.app_key for d in self._existing],
            ),
            display_name=display or raw_key,
            process_names=[] if is_bundle_id else [raw_key],
            bundle_ids=[raw_key] if is_bundle_id else [],
        )
        self._result = spec
        self._close()

    # ---- Web tab --------------------------------------------------------

    def _build_web_tab(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text="Paste a meeting URL",
            background=BG, foreground=INK, font=theme.FONT_H3, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            parent,
            text="Copy the URL from the browser tab of a meeting you run "
                 "regularly. Sayzo will ask to start coaching whenever you "
                 "open that site.",
            background=BG, foreground=MUTED, font=theme.FONT_SMALL,
            anchor="w", justify="left", wraplength=560,
        ).pack(anchor="w", pady=(PAD_XS, PAD_SM))

        tk.Label(
            parent, text="URL",
            background=BG, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XS))
        entry = ttk.Entry(parent, textvariable=self._web_url_var)
        entry.pack(fill="x")
        self._web_url_var.trace_add("write", lambda *_: self._refresh_web_preview())

        # Live preview card.
        preview_card = RoundedFrame(
            parent, fill=SURFACE, outline=BORDER, outline_width=1,
            padx=PAD_MD, pady=PAD_SM,
        )
        preview_card.pack(fill="x", pady=(PAD_SM, PAD_SM))
        tk.Label(
            preview_card.inner,
            text="Will match:",
            background=SURFACE, foreground=MUTED, font=theme.FONT_SMALL, anchor="w",
        ).pack(anchor="w")
        tk.Label(
            preview_card.inner,
            textvariable=self._web_preview_var,
            background=SURFACE, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
            wraplength=480, justify="left",
        ).pack(anchor="w")
        preview_card.fit()
        # Remember the card so the preview updater can re-fit after text changes.
        self._web_preview_card = preview_card

        # Strict toggle.
        strict_row = tk.Frame(parent, background=BG)
        strict_row.pack(fill="x", pady=(PAD_SM, PAD_SM))
        ttk.Checkbutton(
            strict_row,
            text="Only match this exact meeting (not every meeting on the site)",
            variable=self._web_strict_var,
            style="Sayzo.TCheckbutton",
            command=self._refresh_web_preview,
        ).pack(anchor="w")

        # Display name.
        tk.Label(
            parent, text="Name it",
            background=BG, foreground=INK, font=theme.FONT_BODY_BOLD, anchor="w",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XS))
        tk.Label(
            parent,
            text="How this appears in your Meeting Apps list. Auto-filled "
                 "from the URL — change it to whatever's easiest to recognize "
                 "(e.g. “Work standup”, “Client calls”).",
            background=BG, foreground=MUTED, font=theme.FONT_SMALL,
            anchor="w", justify="left", wraplength=520,
        ).pack(anchor="w", pady=(0, PAD_XS))
        ttk.Entry(parent, textvariable=self._web_name_var).pack(fill="x")

        # Validation status line.
        tk.Label(
            parent, textvariable=self._web_status_var,
            background=BG, foreground=ERROR, font=theme.FONT_SMALL,
            anchor="w", justify="left", wraplength=560,
        ).pack(anchor="w", pady=(PAD_SM, 0))

        self._refresh_web_preview()

    def _refresh_web_preview(self) -> None:
        url = self._web_url_var.get().strip()
        if not url:
            self._web_preview_var.set("Paste a URL above to see what it'll match.")
            if getattr(self, "_web_preview_card", None) is not None:
                try:
                    self._web_preview_card.fit()
                except tk.TclError:
                    pass
            return
        parsed = _parse_meeting_url(url)
        if parsed is None:
            self._web_preview_var.set("⚠️  That doesn't look like a meeting URL.")
        else:
            host, path = parsed
            strict = self._web_strict_var.get()
            if strict and not path:
                self._web_preview_var.set(
                    "⚠️  Strict match needs a path — paste a full meeting "
                    "URL, or uncheck “Only match this exact meeting”."
                )
            elif strict:
                self._web_preview_var.set(f"{host}{path} — this exact meeting only")
            else:
                self._web_preview_var.set(f"{host}/… — any meeting on this site")
            # Auto-fill display name the first time (don't clobber user edits).
            if not self._web_name_var.get().strip():
                self._web_name_var.set(_display_name_from_host(host))
        # Resize the preview card so wrapping text fits.
        if getattr(self, "_web_preview_card", None) is not None:
            try:
                self._web_preview_card.fit()
            except tk.TclError:
                pass

    # ---- tab switching --------------------------------------------------

    def _switch_tab(self, tab: str) -> None:
        if tab == self._active_tab:
            return
        self._active_tab = tab
        assert self._desktop_tab is not None and self._web_tab is not None
        assert self._tab_desktop_btn is not None and self._tab_web_btn is not None
        # Visual selected/unselected: swap RoundedButton variants. The
        # widget recreates the background image on variant switch, so we
        # destroy and re-create the button.
        desktop_selected = tab == "desktop"
        self._rebuild_tab_buttons(desktop_selected)
        if desktop_selected:
            self._web_tab.pack_forget()
            self._desktop_tab.pack(fill="both", expand=True)
        else:
            self._desktop_tab.pack_forget()
            self._web_tab.pack(fill="both", expand=True)

    def _rebuild_tab_buttons(self, desktop_selected: bool) -> None:
        # RoundedButton doesn't expose a "change variant" method, so we
        # destroy + recreate. Cheap.
        assert self._tab_desktop_btn is not None and self._tab_web_btn is not None
        parent = self._tab_desktop_btn.master
        self._tab_desktop_btn.destroy()
        self._tab_web_btn.destroy()
        self._tab_desktop_btn = RoundedButton(
            parent, "Desktop app",
            command=lambda: self._switch_tab("desktop"),
            variant="primary" if desktop_selected else "secondary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_desktop_btn.pack(side="left")
        self._tab_web_btn = RoundedButton(
            parent, "Web meeting",
            command=lambda: self._switch_tab("web"),
            variant="secondary" if desktop_selected else "primary",
            padx=PAD_LG, pady=PAD_SM,
        )
        self._tab_web_btn.pack(side="left", padx=(PAD_SM, 0))

    # ---- submit / cancel ------------------------------------------------

    def _on_submit(self) -> None:
        if self._active_tab == "desktop":
            self._submit_manual()
        else:
            self._submit_web()

    def _submit_manual(self) -> None:
        """Validate + build a spec from the manual entry fields."""
        name = self._manual_name_var.get().strip()
        proc = self._manual_process_var.get().strip()
        bundle = self._manual_bundle_var.get().strip()
        if not name:
            self._desktop_status_var.set("Please enter a display name.")
            if not self._manual_expanded:
                self._toggle_manual()
            return
        if sys.platform == "darwin" and not bundle:
            self._desktop_status_var.set("Please enter a bundle identifier.")
            if not self._manual_expanded:
                self._toggle_manual()
            return
        if sys.platform != "darwin" and not proc:
            self._desktop_status_var.set("Please enter a process name.")
            if not self._manual_expanded:
                self._toggle_manual()
            return
        key_material = bundle or proc
        if self._key_already_present(key_material.lower()):
            self._desktop_status_var.set(
                f"“{key_material}” is already on your list.",
            )
            return
        self._result = DetectorSpec(
            app_key=_unique_app_key(
                key_material, [d.app_key for d in self._existing],
            ),
            display_name=name,
            process_names=[proc] if proc else [],
            bundle_ids=[bundle] if bundle else [],
        )
        self._close()

    def _submit_web(self) -> None:
        url = self._web_url_var.get().strip()
        parsed = _parse_meeting_url(url)
        if parsed is None:
            self._web_status_var.set(
                "That doesn't look like a URL — it should have a site like "
                "chatgpt.com or meet.google.com/abc-defg-hij."
            )
            return
        host, path = parsed
        strict = bool(self._web_strict_var.get())
        if strict and not path:
            # Strict without a path would just build the non-strict pattern
            # and mislabel it "this exact meeting only" — reject so the user
            # can either paste the full URL or uncheck strict.
            self._web_status_var.set(
                "“Only match this exact meeting” needs a URL with a path "
                "(e.g. /j/1234567890). Paste the full meeting URL, or "
                "uncheck that option to match the whole site."
            )
            return
        name = self._web_name_var.get().strip() or _display_name_from_host(host)
        pattern = _url_pattern(host, path, strict=strict)
        key_seed = host + (path if strict else "")
        spec = DetectorSpec(
            app_key=_unique_app_key(
                key_seed, [d.app_key for d in self._existing],
            ),
            display_name=name,
            is_browser=True,
            url_patterns=[pattern],
        )
        self._result = spec
        self._close()

    def _on_cancel(self) -> None:
        self._result = None
        self._close()

    def _close(self) -> None:
        self._cancel_desktop_refresh()
        try:
            self._top.grab_release()
        except tk.TclError:
            pass
        try:
            self._top.destroy()
        except tk.TclError:
            pass


# ---- shared URL / key helpers ---------------------------------------------


_APP_KEY_STRIP = re.compile(r"[^a-z0-9]+")
_APP_KEY_TRIM_SUFFIXES = (".exe", ".app")
_APP_KEY_TRIM_PREFIXES = ("com.", "org.", "us.", "io.", "net.", "co.")


def _unique_app_key(seed: str, taken: list[str]) -> str:
    """Return a stable, sluggy ``app_key`` derived from ``seed``.

    App keys are used for cooldown bucketing and must be unique across the
    whitelist. Strips common executable / bundle-id prefixes + suffixes so
    ``loom.exe`` → ``loom`` and ``com.hnc.Discord`` → ``discord``. On
    collision, appends ``-2``, ``-3``, etc.
    """
    s = seed.lower().strip()
    for suffix in _APP_KEY_TRIM_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    for prefix in _APP_KEY_TRIM_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    base = _APP_KEY_STRIP.sub("-", s).strip("-") or "custom"
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def _parse_meeting_url(url: str) -> Optional[tuple[str, str]]:
    """Extract ``(host, path)`` from a user-pasted meeting URL.

    Returns ``None`` only when the URL has no usable host (empty string,
    ``https://`` with nothing after it, a path-only input like
    ``/just/a/path``). Bare domains like ``chatgpt.com`` are accepted and
    returned as ``(host, "")`` — the caller decides how to treat an empty
    path (non-strict matches the whole host either way; strict needs a
    path and should reject an empty one at submit time).
    """
    if not url:
        return None
    # Allow bare ``meet.google.com/abc-defg-hij`` (no scheme).
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        return None
    path = parsed.path or ""
    if path == "/":
        path = ""
    # Strip trailing slash for consistent regex building.
    if path.endswith("/"):
        path = path.rstrip("/")
    return host, path


def _url_pattern(host: str, path: str, *, strict: bool) -> str:
    """Build a URL regex that matches ``host/path`` (or the whole host
    section when ``strict=False``).

    Strict matches the exact meeting room (single room users). Non-strict
    matches any path under the first path segment — e.g. Google Meet's
    ``meet.google.com/abc-defg-hij`` becomes ``^https://meet\\.google\\.com/``
    so every room on the site counts.
    """
    host_re = re.escape(host)
    if strict:
        path_re = re.escape(path)
        return rf"^https://{host_re}{path_re}"
    # Non-strict: anchor to the host, accept any path.
    return rf"^https://{host_re}/"


def _display_name_from_host(host: str) -> str:
    """Guess a display name from a hostname — used to pre-fill the web
    tab's name field.

    ``meet.google.com`` → ``Google Meet``; ``zoom.us`` → ``Zoom``;
    ``whereby.com`` → ``Whereby``. Falls back to the middle hostname label
    for unknown sites.
    """
    known = {
        "meet.google.com": "Google Meet",
        "teams.microsoft.com": "Microsoft Teams",
        "teams.live.com": "Microsoft Teams",
        "zoom.us": "Zoom",
        "whereby.com": "Whereby",
        "meet.jit.si": "Jitsi Meet",
        "8x8.vc": "8x8 Meet",
    }
    h = host.lower()
    if h in known:
        return known[h]
    for k, v in known.items():
        if h.endswith("." + k) or h.endswith(k):
            return v
    parts = [p for p in h.split(".") if p not in ("www", "app", "meet")]
    if len(parts) >= 2:
        base = parts[-2]  # site-ish label for 3+ label hosts
    elif len(parts) == 1:
        base = parts[0]   # short host like "tryclassroom.app"
    else:
        return h
    return base[:1].upper() + base[1:]


def _friendly_url_pattern(pattern: str) -> str:
    """Strip regex glyphs from a URL pattern for display.

    The stored patterns look like ``^https://meet\\.google\\.com/``; users
    shouldn't have to read regex. Returns ``meet.google.com/…``. Best-effort
    — unrecognised patterns fall back to the raw string so we never render
    garbage.
    """
    out = pattern
    # Strip the anchor + scheme.
    for prefix in ("^https://", "^http://", "https://", "http://", "^"):
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    # ``[^/]+`` in subdomain position (between scheme and next ``\.``) → ``*``.
    out = re.sub(r"\[\^/\]\+(?=\\?\.)", "*", out)
    # Remaining char classes (with quantifier) → ellipsis.
    out = re.sub(r"\[[^\]]+\][+*]?(?:\{\d+,?\d*\})?", "…", out)
    # Bare quantifier (no preceding class) → ellipsis.
    out = re.sub(r"\{\d+,?\d*\}", "…", out)
    # Escaped punctuation → literal.
    out = out.replace("\\.", ".").replace("\\-", "-").replace("\\/", "/")
    # ``.+`` / ``.*`` / ``\\d+`` / ``\\w+`` → ellipsis.
    out = out.replace(".+", "…").replace(".*", "…")
    out = re.sub(r"\\[dws]\+", "…", out)
    # Trailing regex anchor ``$`` and trailing slash.
    out = out.rstrip("$").rstrip("/")
    # Collapse adjacent ellipses.
    out = re.sub(r"(?:…[-/]?){2,}", "…", out)
    if not out:
        return pattern
    return out
