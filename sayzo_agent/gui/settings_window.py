"""Settings window for the running agent — four panes, tkinter-hosted.

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
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import TYPE_CHECKING, Optional

from .. import settings_store
from . import theme
from .shortcut_capture import ShortcutCaptureField
from .widgets import RoundedButton
from .theme import (
    ACCENT,
    ACCENT_TINT,
    BG,
    BORDER,
    INK,
    MUTED,
    PAD_LG,
    PAD_MD,
    PAD_SM,
    PAD_XL,
    PAD_XS,
    PAD_XXL,
    SELECTED,
    SURFACE,
    apply_sayzo_icon,
    apply_sayzo_theme,
    make_divider,
)

if TYPE_CHECKING:
    from ..arm.controller import ArmController
    from ..config import Config

log = logging.getLogger(__name__)


PANE_NAMES = ("Shortcut", "Permissions", "Account", "Notifications")


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

    WINDOW_SIZE = (780, 540)
    MIN_SIZE = (680, 460)
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
    and persist to user_settings.json."""

    def __init__(self, parent: tk.Widget, cfg: "Config") -> None:
        super().__init__(parent)
        self._cfg = cfg

        self._master_var = tk.BooleanVar(value=cfg.notifications_enabled)
        self._welcome_var = tk.BooleanVar(value=cfg.notify_welcome)
        self._post_arm_var = tk.BooleanVar(value=cfg.arm.notify_post_arm)
        self._saved_var = tk.BooleanVar(value=cfg.notify_capture_saved)

        ttk.Label(
            self._frame, text="Notifications", style="H1.Sayzo.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self._frame,
            text="Choose which Sayzo toasts show up on your desktop.",
            style="Muted.Sayzo.TLabel",
        ).pack(anchor="w", pady=(PAD_SM, PAD_XL))

        master = ttk.Checkbutton(
            self._frame,
            text="Show Sayzo notifications",
            style="Sayzo.TCheckbutton",
            variable=self._master_var,
            command=self._on_toggle,
        )
        master.pack(anchor="w")

        sub = ttk.Frame(self._frame, style="Sayzo.TFrame")
        sub.pack(anchor="w", padx=(PAD_XL, 0), pady=(PAD_SM, PAD_LG))
        ttk.Checkbutton(
            sub,
            text="Show the welcome message on first launch",
            style="Sayzo.TCheckbutton",
            variable=self._welcome_var,
            command=self._on_toggle,
        ).pack(anchor="w")
        ttk.Checkbutton(
            sub,
            text="Show “Sayzo is capturing” reminders after I arm",
            style="Sayzo.TCheckbutton",
            variable=self._post_arm_var,
            command=self._on_toggle,
        ).pack(anchor="w")
        ttk.Checkbutton(
            sub,
            text="Show “Conversation saved” when a capture finishes",
            style="Sayzo.TCheckbutton",
            variable=self._saved_var,
            command=self._on_toggle,
        ).pack(anchor="w")

        ttk.Label(
            self._frame,
            text="Consent prompts and end-of-meeting questions always show — "
                 "they're how you decide what Sayzo captures.",
            style="Small.Sayzo.TLabel",
            wraplength=480, justify="left",
        ).pack(anchor="w", pady=(PAD_MD, 0))

    def _on_toggle(self) -> None:
        master = bool(self._master_var.get())
        welcome = bool(self._welcome_var.get())
        post_arm = bool(self._post_arm_var.get())
        saved = bool(self._saved_var.get())

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
