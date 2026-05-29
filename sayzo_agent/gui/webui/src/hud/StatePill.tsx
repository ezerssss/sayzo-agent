import { memo } from "react";
import { Check, Minimize2 } from "lucide-react";
import { usePaintedSignal } from "../lib/usePaintedSignal";
import logoSvgUrl from "../assets/logo.svg";
import { GripDots } from "./GripDots";
import { Waveform } from "./Waveform";

interface Props {
  /** Per-show identifier from launcher's show_pill. Mount-effect emits
      it on `card_painted` so the launcher can log delta_ms. Absent
      under the dev-preview path that doesn't go through launcher. */
  paintId?: string;
  audioLevel?: number;
  onStop: () => void;
  onCollapse: () => void;
}

function StatePillImpl({ paintId, audioLevel, onStop, onCollapse }: Props) {
  usePaintedSignal(paintId);

  return (
    <div
      // Content-driven width. Whole pill is the drag region via
      // `hud-drag`; the GripDots row is a visible affordance the user
      // requested. Done/minimize opt out with `hud-no-drag`.
      className="hud-element-enter hud-drag flex flex-col gap-1 rounded-2xl border border-ink-border bg-white px-1 pb-2 pt-1 text-ink shadow-lg"
      style={{ pointerEvents: "auto" }}
    >
      <GripDots />
      <div className="flex items-center gap-2.5 px-2">
        <img
          src={logoSvgUrl}
          alt="Sayzo"
          width={36}
          height={36}
          className="hud-logo-img animate-sayzo-pulse shrink-0"
        />
        <Waveform level={audioLevel} />
        <button
          type="button"
          onClick={onStop}
          title="End coaching session"
          className="hud-no-drag flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1.5 text-[12px] font-semibold text-ink transition hover:bg-accent hover:text-white"
        >
          Done
          <Check size={13} strokeWidth={2.5} />
        </button>
        <button
          type="button"
          onClick={onCollapse}
          title="Collapse"
          className="hud-no-drag flex h-7 w-7 items-center justify-center rounded-full text-ink-muted transition hover:bg-gray-100 hover:text-ink"
        >
          <Minimize2 size={13} />
        </button>
      </div>
    </div>
  );
}

export const StatePill = memo(StatePillImpl);
