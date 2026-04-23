"""Custom tkinter widgets matching the installer's visual language.

Tkinter's built-in ``ttk.Button`` can't render rounded corners portably,
and drawing them on a ``tk.Canvas`` via polygon smoothing produces a
B-spline approximation that reads as subtly-wrong next to the installer's
CSS-rendered ``rounded-md`` buttons. This module uses Pillow to render
the background as an anti-aliased image per state, then composites text
on top with a native Canvas ``create_text`` call.

:class:`RoundedButton` supports the four variants the installer uses:

* ``primary``   — solid Sayzo blue, white text (the main CTA).
* ``secondary`` — white fill, ink text, slate border.
* ``ghost``     — transparent fill, muted text.
* ``danger``    — white fill, red text.

Hover / pressed / disabled / focused states swap in different background
images. A focus ring is drawn when the widget gains keyboard focus, and
``<Return>`` / ``<space>`` activate the command. The per-variant images
are cached per-button so repeat renders are free.

:class:`RoundedFrame` is the same idea without the text or click
handling — used for decorative surfaces like the shortcut "pill".
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageTk

from . import theme
from .theme import (
    ACCENT,
    ACCENT_ACTIVE,
    ACCENT_HOVER,
    ACCENT_RING,
    BG,
    BORDER,
    BORDER_STRONG,
    ERROR,
    INK,
    MUTED,
    SUBTLE,
    SURFACE,
)

log = logging.getLogger(__name__)


# Corner radius in CSS pixels — matches Tailwind's ``rounded-md`` that the
# installer uses for every button.
_RADIUS = 6
# Supersample factor for rendering. Drawing at 3× + downsampling via
# bicubic gives visibly smoother corners on low-DPI Windows displays than
# PIL's built-in antialiasing alone.
_SS = 3


# Variant → state-dependent colors + text font. ``font_key`` maps to a
# theme font tuple resolved at render time.
_VARIANTS: dict[str, dict] = {
    "primary": {
        "fill":          {"normal": ACCENT,  "hover": ACCENT_HOVER, "pressed": ACCENT_ACTIVE, "disabled": "#93c5fd"},
        "text":          {"normal": "#ffffff", "hover": "#ffffff",  "pressed": "#ffffff",      "disabled": "#eff6ff"},
        "outline":       {"normal": None,    "hover": None,         "pressed": None,           "disabled": None},
        "outline_width": 0,
        "font_key":      "bold",
    },
    "secondary": {
        "fill":          {"normal": BG,      "hover": SURFACE,      "pressed": BORDER,       "disabled": SURFACE},
        "text":          {"normal": INK,     "hover": INK,          "pressed": INK,          "disabled": SUBTLE},
        "outline":       {"normal": BORDER,  "hover": BORDER_STRONG, "pressed": BORDER_STRONG, "disabled": BORDER},
        "outline_width": 1,
        "font_key":      "body",
    },
    "ghost": {
        "fill":          {"normal": None,    "hover": SURFACE,      "pressed": BORDER,       "disabled": None},
        "text":          {"normal": MUTED,   "hover": INK,          "pressed": INK,          "disabled": SUBTLE},
        "outline":       {"normal": None,    "hover": None,         "pressed": None,         "disabled": None},
        "outline_width": 0,
        "font_key":      "body",
    },
    "danger": {
        "fill":          {"normal": BG,      "hover": "#fef2f2",    "pressed": "#fee2e2",    "disabled": SURFACE},
        "text":          {"normal": ERROR,   "hover": ERROR,        "pressed": ERROR,        "disabled": SUBTLE},
        "outline":       {"normal": BORDER,  "hover": ERROR,        "pressed": ERROR,        "disabled": BORDER},
        "outline_width": 1,
        "font_key":      "body",
    },
}


def _hex_to_rgba(color: str) -> tuple[int, int, int, int]:
    """Parse ``#rrggbb`` into an opaque RGBA tuple."""
    c = color.lstrip("#")
    if len(c) != 6:
        raise ValueError(f"expected #rrggbb, got {color!r}")
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    return (r, g, b, 255)


def _render_rounded_rect(
    w: int, h: int, r: int, *,
    fill: Optional[str], outline: Optional[str], outline_width: int,
    bg: str,
) -> Image.Image:
    """Render a rounded rectangle via Pillow at a supersampled resolution
    then downsample with ``LANCZOS`` for smooth antialiased edges.

    Transparent fill / outline positions fall back to the canvas bg so the
    downsampled image composites cleanly onto the tkinter canvas.
    """
    # Supersample canvas for AA.
    W, H, R = w * _SS, h * _SS, r * _SS
    # Start from the canvas bg so corners outside the rounded shape blend
    # into the host surface (white by default, SURFACE on the sidebar).
    img = Image.new("RGBA", (W, H), _hex_to_rgba(bg))
    draw = ImageDraw.Draw(img)

    fill_rgba = _hex_to_rgba(fill) if fill else _hex_to_rgba(bg)
    outline_rgba = _hex_to_rgba(outline) if outline else None

    draw.rounded_rectangle(
        [(0, 0), (W - 1, H - 1)],
        radius=R,
        fill=fill_rgba,
        outline=outline_rgba,
        width=outline_width * _SS if outline_rgba else 0,
    )
    return img.resize((w, h), Image.LANCZOS)


def _resolve_font(font_key: str) -> tuple:
    if font_key == "bold":
        return theme.FONT_BODY_BOLD
    return theme.FONT_BODY


# ---------------------------------------------------------------------------
# RoundedButton
# ---------------------------------------------------------------------------


class RoundedButton(tk.Canvas):
    """Canvas-hosted rounded button.

    Parameters:
        parent: tkinter parent widget.
        text: label rendered in the center.
        command: ``Callable[[], None]`` invoked on click or keyboard
            activation. Ignored when the button is disabled.
        variant: ``"primary"`` / ``"secondary"`` / ``"ghost"`` / ``"danger"``.
        radius: corner radius in pixels. Defaults to 6 to match
            Tailwind's ``rounded-md``.
        padx / pady: internal padding around the text.
        bg: background color of the host surface. Defaults to the theme's
            white. Pass ``SURFACE`` when placing the button on the sidebar.
        min_width: minimum pixel width — lets adjacent buttons line up.
        state: ``"normal"`` or ``"disabled"``.
    """

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Optional[Callable[[], None]] = None,
        *,
        variant: str = "primary",
        radius: int = _RADIUS,
        padx: int = 16,
        pady: int = 8,
        bg: Optional[str] = None,
        min_width: Optional[int] = None,
        state: str = "normal",
    ) -> None:
        if variant not in _VARIANTS:
            raise ValueError(f"unknown variant: {variant}")
        self._variant = variant
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._min_width = min_width
        self._command = command
        self._text = text
        self._state = "disabled" if state == "disabled" else "normal"
        self._visual_state = self._state
        self._focused = False
        self._bg = bg if bg is not None else BG

        spec = _VARIANTS[variant]
        self._font_spec = _resolve_font(spec["font_key"])
        self._font = tkfont.Font(
            family=self._font_spec[0],
            size=self._font_spec[1],
            weight=self._font_spec[2] if len(self._font_spec) > 2 else "normal",
        )

        text_w, text_h = self._measure_text(text)
        width = max(text_w + 2 * padx, min_width or 0)
        height = text_h + 2 * pady

        super().__init__(
            parent,
            width=width,
            height=height,
            background=self._bg,
            highlightthickness=0,
            bd=0,
            cursor="arrow" if self._state == "disabled" else "hand2",
            takefocus=1,
        )

        self._image_cache: dict[str, ImageTk.PhotoImage] = {}
        self._render()

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", self._on_activate)
        self.bind("<space>", self._on_activate)
        self.bind("<Configure>", self._on_configure)

    # ---- public API ----------------------------------------------------

    def set_text(self, text: str) -> None:
        self._text = text
        text_w, text_h = self._measure_text(text)
        width = max(text_w + 2 * self._padx, self._min_width or 0)
        height = text_h + 2 * self._pady
        self._image_cache.clear()
        super().configure(width=width, height=height)
        self._render()

    def configure(self, **kwargs) -> None:  # type: ignore[override]
        state = kwargs.pop("state", None)
        super().configure(**kwargs)
        if state is not None:
            self._state = "disabled" if state == "disabled" else "normal"
            self._visual_state = self._state
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
            self._image_cache.clear()
            self._render()

    # ---- event handlers -----------------------------------------------

    def _on_enter(self, _event: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._visual_state = "hover"
        self._render()

    def _on_leave(self, _event: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._visual_state = "normal"
        self._render()

    def _on_press(self, _event: tk.Event) -> None:
        if self._state == "disabled":
            return
        self._visual_state = "pressed"
        self._render()
        self.focus_set()

    def _on_release(self, event: tk.Event) -> None:
        if self._state == "disabled":
            return
        x, y = event.x, event.y
        in_bounds = 0 <= x <= int(self["width"]) and 0 <= y <= int(self["height"])
        self._visual_state = "hover" if in_bounds else "normal"
        self._render()
        if in_bounds and self._command is not None:
            self._invoke()

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._focused = True
        self._render()

    def _on_focus_out(self, _event: tk.Event) -> None:
        self._focused = False
        self._render()

    def _on_activate(self, _event: tk.Event) -> str:
        if self._state == "disabled" or self._command is None:
            return "break"
        self._invoke()
        return "break"

    def _on_configure(self, _event: tk.Event) -> None:
        # Host resized us (rare for a button, but handle it). Invalidate
        # cached images so they re-render at the new size.
        self._image_cache.clear()
        self._render()

    def _invoke(self) -> None:
        try:
            self._command()
        except Exception:
            log.warning("[RoundedButton] command raised", exc_info=True)

    # ---- rendering -----------------------------------------------------

    def _measure_text(self, text: str) -> tuple[int, int]:
        return self._font.measure(text), self._font.metrics("linespace")

    def _bg_image(self, state: str) -> ImageTk.PhotoImage:
        cached = self._image_cache.get(state)
        if cached is not None:
            return cached
        spec = _VARIANTS[self._variant]
        w = int(self["width"])
        h = int(self["height"])
        img = _render_rounded_rect(
            w, h, self._radius,
            fill=spec["fill"][state],
            outline=spec["outline"][state],
            outline_width=spec["outline_width"],
            bg=self._bg,
        )
        photo = ImageTk.PhotoImage(img)
        self._image_cache[state] = photo
        return photo

    def _render(self) -> None:
        self.delete("all")
        spec = _VARIANTS[self._variant]
        w = int(self["width"])
        h = int(self["height"])
        state = (
            "disabled" if self._state == "disabled"
            else self._visual_state
        )

        # Background (rounded fill + optional border).
        self.create_image(0, 0, anchor="nw", image=self._bg_image(state))

        # Focus ring — a second rounded rect inset by 3px with a translucent
        # accent outline. Only shown when focused and not disabled.
        if self._focused and self._state != "disabled":
            inset = 3
            ring_img = _render_rounded_rect(
                w - 2 * inset, h - 2 * inset, max(1, self._radius - inset),
                fill=None,
                outline=ACCENT_RING,
                outline_width=2,
                bg=self._bg,
            )
            ring_photo = ImageTk.PhotoImage(ring_img)
            self._image_cache[f"_focus_{state}"] = ring_photo
            self.create_image(inset, inset, anchor="nw", image=ring_photo)

        # Text.
        text_color = spec["text"][state]
        self.create_text(
            w // 2, h // 2,
            text=self._text,
            fill=text_color,
            font=self._font_spec,
        )


# ---------------------------------------------------------------------------
# RoundedFrame — decorative container (no click handling). Used for the
# shortcut "pill" so it picks up the same radius as the buttons.
# ---------------------------------------------------------------------------


class RoundedFrame(tk.Canvas):
    """A non-interactive rounded container, sized to fit a single child.

    The child is placed inside with the provided padding. The Canvas does
    not manage layout beyond positioning one inner frame, so the caller
    packs widgets into ``frame.inner`` and calls ``frame.fit()`` if the
    inner contents changed size.
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        radius: int = _RADIUS,
        padx: int = 12,
        pady: int = 6,
        fill: Optional[str] = BG,
        outline: Optional[str] = BORDER,
        outline_width: int = 1,
        bg: Optional[str] = None,
    ) -> None:
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._fill = fill
        self._outline = outline
        self._outline_width = outline_width
        self._bg = bg if bg is not None else BG
        super().__init__(
            parent,
            background=self._bg,
            highlightthickness=0,
            bd=0,
        )
        self.inner = tk.Frame(self, background=fill or self._bg, bd=0, highlightthickness=0)
        self._inner_id: Optional[int] = None
        self._photo: Optional[ImageTk.PhotoImage] = None

    def fit(self) -> None:
        """Resize the canvas to its inner content + padding, then redraw."""
        self.inner.update_idletasks()
        w = self.inner.winfo_reqwidth() + 2 * self._padx
        h = self.inner.winfo_reqheight() + 2 * self._pady
        super().configure(width=w, height=h)
        self.delete("all")
        img = _render_rounded_rect(
            w, h, self._radius,
            fill=self._fill,
            outline=self._outline,
            outline_width=self._outline_width,
            bg=self._bg,
        )
        self._photo = ImageTk.PhotoImage(img)
        self.create_image(0, 0, anchor="nw", image=self._photo)
        if self._inner_id is None:
            self._inner_id = self.create_window(
                self._padx, self._pady, anchor="nw", window=self.inner,
            )
        else:
            self.coords(self._inner_id, self._padx, self._pady)

    def set_outline(self, color: Optional[str]) -> None:
        """Change the border color and redraw in place."""
        if color == self._outline:
            return
        self._outline = color
        self.fit()
