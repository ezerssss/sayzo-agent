"""Shared design tokens + ttk theme for Sayzo's runtime GUI windows.

Mirrors the installer's Tailwind palette (``gui/webui/tailwind.config.js``)
so Settings + Onboarding feel like the same product as the first-run
installer — same colors, same font stack, same spacing rhythm.

Usage::

    from .theme import apply_sayzo_theme, PAD_LG, FONT_H1

    root = tk.Tk()
    apply_sayzo_theme(root)
    ttk.Label(root, text="Hello", style="H1.Sayzo.TLabel").pack(padx=PAD_LG)
    ttk.Button(root, text="Continue", style="Primary.Sayzo.TButton").pack()

The ``ttk`` style engine uses the "clam" theme as its base because it is
the only built-in theme that reliably honors per-widget background
overrides on Windows + macOS + Linux. Native themes (``vista``,
``aqua``) ignore ``background=`` on buttons, which would leave us with
the default OS look after all our work.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from tkinter import font as tkfont

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Color palette — mirrors installer's tailwind config.
# ---------------------------------------------------------------------------

BG = "#ffffff"           # window background
INK = "#1a1a1a"          # primary text
MUTED = "#6b7280"        # slate-500 — subtext, hints
SUBTLE = "#9ca3af"       # slate-400 — meta, timestamps
BORDER = "#e5e7eb"       # slate-200 — card edges, separators
BORDER_STRONG = "#d1d5db"  # slate-300 — focus ring on inputs
SURFACE = "#f9fafb"      # gray-50 — hover state on secondary buttons, card bg
SELECTED = "#eff6ff"     # blue-50 — sidebar selection background

ACCENT = "#2563eb"       # blue-600 — primary CTA
ACCENT_HOVER = "#1d4ed8"  # blue-700
ACCENT_ACTIVE = "#1e40af"  # blue-800 — pressed
ACCENT_RING = "#93c5fd"  # blue-300 — focus ring
ACCENT_TINT = "#dbeafe"  # blue-100 — very subtle tint

ERROR = "#b91c1c"        # red-700
SUCCESS = "#059669"      # emerald-600


# ---------------------------------------------------------------------------
# Spacing scale (pixels). Use these instead of arbitrary magic numbers.
# ---------------------------------------------------------------------------

PAD_XS = 4
PAD_SM = 8
PAD_MD = 12
PAD_LG = 16
PAD_XL = 24
PAD_XXL = 32
PAD_3XL = 48


# ---------------------------------------------------------------------------
# Typography. ``_font_family`` is set lazily by ``apply_sayzo_theme`` so the
# active Tk instance picks its best available font.
# ---------------------------------------------------------------------------

_FONT_FAMILY_PREFERENCES = {
    "darwin": ("SF Pro Text", "Helvetica Neue", "Arial"),
    "win32": ("Segoe UI Variable", "Segoe UI", "Arial"),
    "linux": ("Inter", "Ubuntu", "DejaVu Sans", "Arial"),
}


def _pick_font_family() -> str:
    """Choose the first available preferred font for the current platform."""
    pref = _FONT_FAMILY_PREFERENCES.get(sys.platform, ("Arial",))
    try:
        available = set(tkfont.families())
    except tk.TclError:
        # No root yet — fall back to the first preference.
        return pref[0]
    for name in pref:
        if name in available:
            return name
    return pref[-1]


# Font tuples are frozen after theme application so every caller sees the
# same values. Populated by ``apply_sayzo_theme``.
FONT_FAMILY: str = "Segoe UI"
FONT_BODY = (FONT_FAMILY, 10)
FONT_BODY_BOLD = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_H1 = (FONT_FAMILY, 18, "bold")
FONT_H2 = (FONT_FAMILY, 13, "bold")
FONT_H3 = (FONT_FAMILY, 11, "bold")
FONT_STEP = (FONT_FAMILY, 9, "bold")  # "STEP 1 OF 5" label


# ---------------------------------------------------------------------------
# Style application
# ---------------------------------------------------------------------------


def apply_sayzo_theme(root: tk.Tk) -> ttk.Style:
    """Configure the shared ttk styles + root window defaults.

    Idempotent — safe to call more than once on the same root (each
    subsequent call just re-applies the same definitions).
    """
    global FONT_FAMILY, FONT_BODY, FONT_BODY_BOLD, FONT_SMALL
    global FONT_H1, FONT_H2, FONT_H3, FONT_STEP

    family = _pick_font_family()
    FONT_FAMILY = family
    FONT_BODY = (family, 10)
    FONT_BODY_BOLD = (family, 10, "bold")
    FONT_SMALL = (family, 9)
    FONT_H1 = (family, 18, "bold")
    FONT_H2 = (family, 13, "bold")
    FONT_H3 = (family, 11, "bold")
    FONT_STEP = (family, 9, "bold")

    root.configure(background=BG)
    try:
        # Named TkDefaultFont is applied to widgets that read it implicitly.
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=family, size=10)
        text_font = tkfont.nametofont("TkTextFont")
        text_font.configure(family=family, size=10)
    except tk.TclError:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        # 'clam' is bundled with standard CPython; should never fail, but
        # leave the user's theme alone if it does.
        pass

    # ---- generic containers ------------------------------------------
    style.configure("Sayzo.TFrame", background=BG)
    style.configure("Card.Sayzo.TFrame", background=BG)
    style.configure("Surface.Sayzo.TFrame", background=SURFACE)
    style.configure("Sidebar.Sayzo.TFrame", background=SURFACE)

    # ---- labels -------------------------------------------------------
    style.configure(
        "Sayzo.TLabel", background=BG, foreground=INK, font=FONT_BODY,
    )
    style.configure(
        "H1.Sayzo.TLabel", background=BG, foreground=INK, font=FONT_H1,
    )
    style.configure(
        "H2.Sayzo.TLabel", background=BG, foreground=INK, font=FONT_H2,
    )
    style.configure(
        "H3.Sayzo.TLabel", background=BG, foreground=INK, font=FONT_H3,
    )
    style.configure(
        "Muted.Sayzo.TLabel", background=BG, foreground=MUTED, font=FONT_BODY,
    )
    style.configure(
        "Small.Sayzo.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL,
    )
    style.configure(
        "Step.Sayzo.TLabel",
        background=BG, foreground=ACCENT, font=FONT_STEP,
    )
    style.configure(
        "Error.Sayzo.TLabel",
        background=BG, foreground=ERROR, font=FONT_BODY,
    )
    style.configure(
        "Success.Sayzo.TLabel",
        background=BG, foreground=SUCCESS, font=FONT_BODY,
    )
    # Labels inside the gray sidebar — background differs.
    style.configure(
        "Sidebar.Muted.Sayzo.TLabel",
        background=SURFACE, foreground=MUTED, font=FONT_SMALL,
    )
    style.configure(
        "Sidebar.H2.Sayzo.TLabel",
        background=SURFACE, foreground=INK, font=FONT_H2,
    )

    # ---- buttons ------------------------------------------------------
    # Primary — blue fill. Note: clam renders `padding` as (x, y) tuple in px.
    style.configure(
        "Primary.Sayzo.TButton",
        background=ACCENT,
        foreground="#ffffff",
        bordercolor=ACCENT,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        focuscolor=ACCENT_RING,
        borderwidth=0,
        relief="flat",
        padding=(PAD_LG, PAD_SM),
        font=FONT_BODY_BOLD,
    )
    style.map(
        "Primary.Sayzo.TButton",
        background=[
            ("pressed", ACCENT_ACTIVE),
            ("active", ACCENT_HOVER),
            ("disabled", "#93c5fd"),
        ],
        foreground=[("disabled", "#eff6ff")],
    )

    # Secondary — white fill, ink text, gray border, subtle hover.
    style.configure(
        "Secondary.Sayzo.TButton",
        background=BG,
        foreground=INK,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        focuscolor=ACCENT_RING,
        borderwidth=1,
        relief="solid",
        padding=(PAD_LG, PAD_SM - 1),
        font=FONT_BODY,
    )
    style.map(
        "Secondary.Sayzo.TButton",
        background=[
            ("pressed", BORDER),
            ("active", SURFACE),
            ("disabled", SURFACE),
        ],
        foreground=[("disabled", SUBTLE)],
        bordercolor=[("active", BORDER_STRONG)],
    )

    # Ghost — no fill, muted ink text. Used for Cancel / Skip for now.
    style.configure(
        "Ghost.Sayzo.TButton",
        background=BG,
        foreground=MUTED,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        focuscolor=ACCENT_RING,
        borderwidth=0,
        relief="flat",
        padding=(PAD_LG, PAD_SM),
        font=FONT_BODY,
    )
    style.map(
        "Ghost.Sayzo.TButton",
        background=[("active", SURFACE), ("pressed", SURFACE)],
        foreground=[("active", INK), ("pressed", INK)],
    )

    # Danger — for destructive actions (Sign out).
    style.configure(
        "Danger.Sayzo.TButton",
        background=BG,
        foreground=ERROR,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        focuscolor=ACCENT_RING,
        borderwidth=1,
        relief="solid",
        padding=(PAD_LG, PAD_SM - 1),
        font=FONT_BODY,
    )
    style.map(
        "Danger.Sayzo.TButton",
        background=[("active", "#fef2f2"), ("pressed", "#fee2e2")],
        bordercolor=[("active", ERROR)],
    )

    # ---- checkbuttons -------------------------------------------------
    style.configure(
        "Sayzo.TCheckbutton",
        background=BG,
        foreground=INK,
        font=FONT_BODY,
        padding=(0, PAD_XS),
    )
    style.map(
        "Sayzo.TCheckbutton",
        background=[("active", BG)],
    )

    # ---- separators ---------------------------------------------------
    style.configure("Sayzo.TSeparator", background=BORDER)

    # ---- scrollbars ---------------------------------------------------
    # Thin flat scrollbar — no bulky arrow buttons, matches the slate
    # palette. Clam exposes enough style options for this; native themes
    # on Windows/macOS ignore half of these so we rely on the "clam" base
    # theme being set above.
    style.layout(
        "Sayzo.Vertical.TScrollbar",
        [(
            "Vertical.Scrollbar.trough",
            {"sticky": "ns", "children": [(
                "Vertical.Scrollbar.thumb",
                {"expand": "1", "sticky": "nswe"},
            )]},
        )],
    )
    style.configure(
        "Sayzo.Vertical.TScrollbar",
        gripcount=0,
        background=BORDER_STRONG,
        troughcolor=SURFACE,
        bordercolor=SURFACE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        arrowcolor=SURFACE,  # unused given the layout, but belt-and-braces
        relief="flat",
        borderwidth=0,
        arrowsize=0,
        width=10,
    )
    style.map(
        "Sayzo.Vertical.TScrollbar",
        background=[("active", MUTED), ("pressed", MUTED)],
    )

    # ---- notebook (unused for now but set so Onboarding can adopt) ---
    style.configure("Sayzo.TNotebook", background=BG, borderwidth=0)
    style.configure(
        "Sayzo.TNotebook.Tab",
        padding=(PAD_LG, PAD_SM),
        background=BG,
        foreground=MUTED,
        font=FONT_BODY,
    )
    style.map(
        "Sayzo.TNotebook.Tab",
        background=[("selected", BG)],
        foreground=[("selected", INK)],
    )

    return style


# ---------------------------------------------------------------------------
# Helpers for common composed elements. Kept here so every window renders
# them identically.
# ---------------------------------------------------------------------------


def make_page_header(
    parent: tk.Widget,
    title: str,
    subtitle: str | None = None,
    step: str | None = None,
) -> ttk.Frame:
    """Render a page header: optional step indicator, title (H1), subtitle.

    Returns the header frame already packed into ``parent``; further
    content should ``pack`` below.
    """
    header = ttk.Frame(parent, style="Sayzo.TFrame")
    header.pack(fill="x", pady=(0, PAD_LG))
    if step:
        ttk.Label(
            header, text=step.upper(), style="Step.Sayzo.TLabel",
        ).pack(anchor="w", pady=(0, PAD_XS))
    ttk.Label(
        header, text=title, style="H1.Sayzo.TLabel",
    ).pack(anchor="w")
    if subtitle:
        ttk.Label(
            header, text=subtitle, style="Muted.Sayzo.TLabel",
            wraplength=560, justify="left",
        ).pack(anchor="w", pady=(PAD_SM, 0))
    return header


def make_divider(parent: tk.Widget, pady: int = PAD_LG) -> ttk.Separator:
    sep = ttk.Separator(parent, orient="horizontal", style="Sayzo.TSeparator")
    sep.pack(fill="x", pady=pady)
    return sep


# ---------------------------------------------------------------------------
# Window icon — use the installer's Sayzo logo so tkinter windows show up
# with the product icon in the taskbar/dock instead of the default Python
# feather. Frozen builds get the asset from sys._MEIPASS; dev installs from
# the repo's installer/assets directory.
# ---------------------------------------------------------------------------


def _asset_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "installer" / "assets"  # type: ignore[attr-defined]
    # theme.py is sayzo_agent/gui/theme.py — parent.parent.parent is repo root.
    return Path(__file__).resolve().parent.parent.parent / "installer" / "assets"


def apply_sayzo_icon(root: tk.Tk) -> None:
    """Set the Sayzo logo as the window / taskbar icon.

    On Windows we prefer ``logo.ico`` via ``iconbitmap`` — that's what
    the taskbar renders. Everywhere else we fall back to ``logo.png``
    through ``iconphoto`` so the title-bar icon at least matches. Failures
    are logged and swallowed so a missing asset never blocks the window.
    """
    base = _asset_dir()

    if sys.platform == "win32":
        ico = base / "logo.ico"
        if ico.exists():
            try:
                root.iconbitmap(default=str(ico))
                return
            except tk.TclError:
                log.debug("[theme] iconbitmap failed for %s", ico, exc_info=True)

    png = base / "logo.png"
    if not png.exists():
        return
    try:
        photo = tk.PhotoImage(file=str(png))
    except tk.TclError:
        log.debug("[theme] tk.PhotoImage failed for %s", png, exc_info=True)
        return
    try:
        root.iconphoto(True, photo)
    except tk.TclError:
        log.debug("[theme] iconphoto failed", exc_info=True)
        return
    # Pin a reference to the PhotoImage on the root so GC doesn't wipe it.
    root._sayzo_icon = photo  # type: ignore[attr-defined]
