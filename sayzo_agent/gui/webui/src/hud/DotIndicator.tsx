import { memo } from "react";
import logoSvgUrl from "../assets/logo.svg";
import { GripDots } from "./GripDots";

interface Props {
  onExpand: () => void;
}

function DotIndicatorImpl({ onExpand }: Props) {
  return (
    <div
      className="hud-drag flex flex-col items-center rounded-2xl border border-ink-border bg-white px-2 pb-1.5 pt-1 shadow-md"
      style={{ pointerEvents: "auto" }}
    >
      <GripDots />
      <button
        type="button"
        onClick={onExpand}
        title="Expand"
        className="hud-no-drag mt-0.5 flex h-14 w-14 items-center justify-center rounded-full transition hover:bg-gray-50"
      >
        <img
          src={logoSvgUrl}
          alt="Sayzo"
          width={44}
          height={44}
          className="hud-logo-img animate-sayzo-pulse"
        />
      </button>
    </div>
  );
}

export const DotIndicator = memo(DotIndicatorImpl);
