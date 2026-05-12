import { ReactNode } from "react";

// Top-level layout for the HUD window. The pywebview window is a
// transparent 400×340 canvas in the top-right; this shell sets the
// click-through behaviour and arranges children in a vertical stack
// from the top-right corner.
//
// Click-through: the root div has `pointer-events: none` so the empty
// regions of the canvas don't intercept clicks (the user can keep
// clicking through to the meeting app underneath). Each child re-enables
// `pointer-events: auto` on itself.
//
// macOS draggable region: the title-bar-equivalent drag handle uses
// `-webkit-app-region: drag` which pywebview's cocoa backend honours on
// WKWebView. Buttons use `-webkit-app-region: no-drag` so clicks still
// register. On Windows WebView2 the same CSS is recognised as a no-op
// — drag is implemented via pywin32 in window.py if/when we wire it.

interface Props {
  children: ReactNode;
}

export function HudShell({ children }: Props) {
  return (
    <div
      className="fixed inset-0 flex flex-col items-end gap-2 p-3"
      style={{ pointerEvents: "none" }}
    >
      {children}
    </div>
  );
}
