import { ReactNode } from "react";
import { GripDots } from "./GripDots";
import { SayzoBrand } from "./SayzoBrand";

interface HudCardProps {
  children: ReactNode;
  width?: number | string;
  className?: string;
}

export function HudCard({
  children,
  width = 340,
  className = "px-4 pb-4 pt-1",
}: HudCardProps) {
  return (
    <div
      className={`hud-element-enter hud-drag relative flex flex-col overflow-hidden rounded-2xl border border-ink-border bg-white text-ink shadow-xl ${className}`}
      style={{ pointerEvents: "auto", width }}
    >
      <GripDots />
      {children}
    </div>
  );
}

interface HudCardBrandHeaderProps {
  size?: number;
  textClassName?: string;
}

export function HudCardBrandHeader({
  size = 32,
  textClassName = "text-[17px]",
}: HudCardBrandHeaderProps) {
  return (
    <div className="mt-1 border-b border-ink-border pb-3">
      <SayzoBrand size={size} textClassName={textClassName} />
    </div>
  );
}
