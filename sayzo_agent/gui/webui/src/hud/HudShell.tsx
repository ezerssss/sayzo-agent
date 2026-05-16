import { forwardRef, ReactNode } from "react";

// Top-level layout for the HUD. `inline-flex flex-col items-end` sizes
// the shell to its widest visible child; `HudApp` observes this shell
// via ref + ResizeObserver and reports the rect back to Qt so the host
// window snaps to the same size.
//
// `.hud-drag` regions are claimed for native window drag by a global
// mousedown listener in `HudApp.tsx` that delegates to Qt's
// `QWindow.startSystemMove`. `.hud-no-drag` opts a child element back
// out so buttons keep their normal click behaviour.
//
// `visible=false` applies the `hud-fade-out` class so the shell fades
// to opacity 0 before `HudApp` tells Qt to move the host offscreen.
//
// Padding (`p-3`) + gap (`gap-2`) only apply while there's content to
// wrap. With no children the shell would otherwise be a 24×24 box (just
// the padding) — invisible against the transparent body but still
// causing ResizeObserver to report 24×24 to Qt, which would size the
// host window to a useless 24×24 idle box. With padding gated on
// `visible` the shell collapses to 0×0 when empty, the Qt size-update
// callback drops the 0×0 report, and the window keeps its previous
// geometry ready for the next content arrival.

interface Props {
  children: ReactNode;
  visible: boolean;
}

export const HudShell = forwardRef<HTMLDivElement, Props>(
  function HudShellImpl({ children, visible }, ref) {
    return (
      <div
        ref={ref}
        className={`hud-fade ${visible ? "hud-fade-in p-3 gap-2" : "hud-fade-out"} inline-flex flex-col items-end`}
        style={{ pointerEvents: "none" }}
      >
        {children}
      </div>
    );
  },
);
