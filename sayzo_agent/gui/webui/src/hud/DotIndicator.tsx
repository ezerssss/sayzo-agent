import { memo } from "react";
import logoSvgUrl from "../assets/logo.svg";
import { GripDots } from "./GripDots";

interface Props {
  onExpand: () => void;
}

function DotIndicatorImpl({ onExpand }: Props) {
  return (
    <div
      // Padding matches `StatePill` (px-1 horizontally / pb-2 below
      // the GripDots) so the dot ends up the same height as the pill
      // — collapse / expand becomes a width-only change with no
      // vertical jump. The button is sized to the same 36×36 the
      // pill's logo uses, so the visual mark stays identical between
      // the two states.
      className="hud-element-enter hud-drag flex flex-col items-center gap-1 rounded-2xl border border-ink-border bg-white px-1 pb-2 pt-1 text-ink shadow-md"
      style={{ pointerEvents: "auto" }}
    >
      <GripDots />
      <button
        type="button"
        onClick={onExpand}
        title="Expand"
        className="hud-no-drag flex h-9 w-9 items-center justify-center rounded-full transition hover:bg-gray-50"
      >
        <img
          src={logoSvgUrl}
          alt="Sayzo"
          width={36}
          height={36}
          className="hud-logo-img animate-sayzo-pulse"
        />
      </button>
    </div>
  );
}

export const DotIndicator = memo(DotIndicatorImpl);
