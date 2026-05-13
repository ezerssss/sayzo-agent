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

interface Props {
  children: ReactNode;
  visible: boolean;
}

export const HudShell = forwardRef<HTMLDivElement, Props>(
  function HudShellImpl({ children, visible }, ref) {
    return (
      <div
        ref={ref}
        className={`hud-fade ${visible ? "hud-fade-in" : "hud-fade-out"} inline-flex flex-col items-end gap-2 p-3`}
        style={{ pointerEvents: "none" }}
      >
        {children}
      </div>
    );
  },
);
