"""Probe: why is the macOS HUD invisible after V8 boots? (v3.1.5 follow-up)

Background — the user's 2026-05-15 v3.1.5 bug
---------------------------------------------
After the v3.1.5 ``allow-jit`` entitlement fix, the HUD subprocess on
macOS now boots V8 cleanly:

    [hud] subprocess emitted hud_ready
    [hud] window visibility → shown (pos=1887,33 size=24x24)
    [hud] window size → 364x232 (visible=True)

…but the user STILL doesn't see the HUD. The window logs success at
positions that should be on-screen (right edge of a 1920x1080 monitor),
yet nothing renders visibly. On Windows the exact same code is fine.

This probe replicates the HudWindow setup OUTSIDE the production
bundle so we can A/B which knob is responsible:

  --mode opaque         : frameless+Tool+NoFocus+StaysOnTop, NO transparency,
                          loads a brightly-coloured HTML test page
  --mode transparent    : same as above + WA_TranslucentBackground (the
                          production setting)
  --mode no-tool        : drop Qt.WindowType.Tool (which becomes NSPanel
                          on macOS); keep everything else
  --mode no-overlay     : skip the macOS NSStatusWindowLevel +
                          collection-behavior tweaks
  --mode minimal        : just a normal QWidget with QWebEngineView, no
                          frameless / no transparency / no level tweaks
                          — baseline "does ANY window render correctly?"

Each mode performs the same animation sequence the production HUD does:
spawn offscreen → snap to top-right at 24x24 (pill) → grow to 364x232
(consent card) → wait 8 s → grow to 420x500 → wait 8 s → quit.

Position is logged at every step. The HTML test page paints a vivid
magenta rectangle so it's *unmissable* if it actually renders.

Usage
-----
On the Mac, in Terminal::

    killall -9 sayzo-agent 2>/dev/null; sleep 1
    python3 scripts/probe_macos_hud_visibility.py --mode opaque
    python3 scripts/probe_macos_hud_visibility.py --mode transparent
    python3 scripts/probe_macos_hud_visibility.py --mode no-tool
    python3 scripts/probe_macos_hud_visibility.py --mode no-overlay
    python3 scripts/probe_macos_hud_visibility.py --mode minimal

Run them one at a time. Each runs ~25 s. Watch the top-right corner
of the screen.

What we're looking for
----------------------
* ``opaque`` shows a magenta rectangle at top-right →
    The window-positioning logic works on macOS. The bug is in
    ``transparent`` mode → WA_TranslucentBackground + QWebEngineView
    is broken on macOS → workaround: use a coloured background with
    chromakey, or move from per-pixel alpha to a real opaque card.

* ``opaque`` ALSO doesn't render →
    Then it's not transparency — it's the frameless+Tool+NoFocus
    combo that's hiding it. Try ``no-tool`` next.

* ``no-tool`` renders but ``opaque`` doesn't →
    Qt.Tool → NSPanel is the culprit. Need to drop Qt.Tool on macOS
    or set additional NSPanel flags.

* ``no-overlay`` renders but our normal mode doesn't →
    The mac overlay tweaks (setLevel_/setHidesOnDeactivate_) are
    interfering. Probably an ordering issue — applied before the
    window is actually mapped.

* ``minimal`` doesn't render →
    Something fundamentally broken with QWebEngineView on this Mac.
    Reinstall PySide6, check architecture (arm64 vs x86_64), check
    Qt resources.

Position logging
----------------
After every move/resize the script prints the actual ``self.x()``,
``self.y()``, ``self.width()``, ``self.height()`` values so we can
tell if Qt is silently clamping/ignoring our setGeometry on macOS
(another known platform difference).
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


# A standalone HTML page with a vivid magenta rectangle that fills
# the viewport. If the window is rendering anything at all, we'll
# see this. If we DON'T see magenta, the window is invisible.
TEST_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Sayzo HUD probe</title></head>
<body style="margin:0;padding:0;background:transparent;">
  <div style="
    background: magenta;
    color: white;
    font: bold 32px sans-serif;
    width: 100vw;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 4px solid yellow;
    box-sizing: border-box;
    text-align: center;
    line-height: 1.2;
  ">
    PROBE<br>VISIBLE
  </div>
</body>
</html>
""".strip()


HUD_EDGE_INSET = 8
INITIAL_W = 100
INITIAL_H = 100


def _offscreen() -> tuple[int, int]:
    return (-20000, 0)


def _compute_top_right_anchor() -> tuple[int, int]:
    """Mirror HudWindow._compute_screen_anchor."""
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return (1000, 50)
    avail = screen.availableGeometry()
    right_x = avail.x() + avail.width() - HUD_EDGE_INSET
    top_y = avail.y() + HUD_EDGE_INSET
    return right_x, top_y


def _apply_mac_overlay_tweaks(widget: QWidget) -> None:
    """Mirror HudWindow._apply_mac_overlay_tweaks (post v3.1.3 fix)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorIgnoresCycle,
            NSWindowCollectionBehaviorTransient,
        )
        import objc  # type: ignore[import-not-found]
    except Exception as e:
        print(f"[probe] AppKit unavailable, skipping mac overlay: {e}")
        return
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        if ns_window is None:
            print("[probe] NSView has no NSWindow yet — overlay skipped")
            return
        ns_window.setLevel_(25)  # NSStatusWindowLevel
        behavior = (
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorTransient
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        ns_window.setCollectionBehavior_(behavior)
        ns_window.setHidesOnDeactivate_(False)
        print("[probe] mac overlay tweaks applied")
    except Exception as e:
        print(f"[probe] mac overlay tweaks failed: {e}")


def build_window(mode: str) -> QWidget:
    w = QWidget()
    flags = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    if mode == "minimal":
        # Plain widget — no flags, no transparency, just a window.
        w.setWindowTitle("HUD probe (minimal)")
    else:
        if mode != "no-tool":
            flags |= Qt.WindowType.Tool
        w.setWindowFlags(flags)
        if mode in ("transparent",):
            w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            w.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        w.setWindowTitle(f"HUD probe ({mode})")

    layout = QVBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    view = QWebEngineView(w)
    if mode == "transparent":
        # Match HudWindow: explicit transparent page background so
        # Chromium doesn't paint white under our React content.
        view.page().setBackgroundColor(QColor(0, 0, 0, 0))
    layout.addWidget(view)

    # Load the test HTML by setting it directly so we don't need a file.
    view.setHtml(TEST_HTML)
    return w


def run(mode: str, wait_secs: float) -> int:
    print("=" * 72)
    print(f"VISIBILITY PROBE — mode={mode}")
    print("=" * 72)

    app = QApplication.instance() or QApplication(sys.argv)

    right_x, top_y = _compute_top_right_anchor()
    print(f"[probe] screen anchor: right_x={right_x} top_y={top_y}")

    widget = build_window(mode)

    # Spawn offscreen, then show — same lifecycle as HudWindow.
    ox, oy = _offscreen()
    widget.setGeometry(ox, oy, INITIAL_W, INITIAL_H)
    widget.show()

    if mode not in ("no-overlay", "minimal"):
        _apply_mac_overlay_tweaks(widget)

    def _log_geom(label: str) -> None:
        print(
            f"[probe] {label}: pos=({widget.x()},{widget.y()}) "
            f"size=({widget.width()}x{widget.height()})"
        )

    _log_geom("after show offscreen")

    # Step 1 — show as 24x24 pill at top-right.
    def step_show_pill() -> None:
        w_, h_ = 24, 24
        x = right_x - w_
        y = top_y
        print(f"\n[probe] STEP 1: show pill at ({x},{y}) size={w_}x{h_}")
        widget.setGeometry(x, y, w_, h_)
        _log_geom("after show pill")

    # Step 2 — grow to 364x232 consent-card size, anchored to right edge.
    def step_grow_card() -> None:
        w_, h_ = 364, 232
        x = right_x - w_
        y = top_y
        print(f"\n[probe] STEP 2: grow to consent card ({x},{y}) size={w_}x{h_}")
        widget.setGeometry(x, y, w_, h_)
        _log_geom("after grow card")

    # Step 3 — bigger card, again anchored.
    def step_grow_big() -> None:
        w_, h_ = 420, 500
        x = right_x - w_
        y = top_y
        print(f"\n[probe] STEP 3: grow to big card ({x},{y}) size={w_}x{h_}")
        widget.setGeometry(x, y, w_, h_)
        _log_geom("after grow big")

    QTimer.singleShot(int(wait_secs * 1000), step_show_pill)
    QTimer.singleShot(int(wait_secs * 1000) + 4000, step_grow_card)
    QTimer.singleShot(int(wait_secs * 1000) + 12000, step_grow_big)
    QTimer.singleShot(int(wait_secs * 1000) + 20000, app.quit)

    print(
        f"\n[probe] waiting {wait_secs}s before STEP 1, then 4s, then 8s, "
        f"then 8s, then quit. Watch the top-right of your screen for a\n"
        f"        magenta rectangle with yellow border that reads 'PROBE VISIBLE'."
    )

    rc = app.exec()
    print(f"\n[probe] event loop exited rc={rc}")
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Reproduce the macOS HUD visibility issue with a tiny test page.",
    )
    p.add_argument(
        "--mode",
        choices=["opaque", "transparent", "no-tool", "no-overlay", "minimal"],
        default="transparent",
        help="Which window setup to test (default: transparent — matches production HUD)",
    )
    p.add_argument(
        "--initial-wait",
        type=float,
        default=2.0,
        help="Seconds to wait offscreen before STEP 1 (default: 2)",
    )
    args = p.parse_args(argv)
    return run(args.mode, args.initial_wait)


if __name__ == "__main__":
    sys.exit(main())
