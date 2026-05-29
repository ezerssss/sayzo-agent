import { useCountdownTimer } from "../lib/useCountdownTimer";
import { usePaintedSignal } from "../lib/usePaintedSignal";
import { HudCard, HudCardBrandHeader } from "./HudCard";

type Answer = "yes" | "no" | "timeout";

interface Props {
  requestId: string;
  title: string;
  body: string;
  yesLabel: string;
  noLabel: string;
  timeoutSecs: number;
  onAnswer: (answer: Answer) => void;
}

export function ConsentCard({
  requestId,
  title,
  body,
  yesLabel,
  noLabel,
  timeoutSecs,
  onAnswer,
}: Props) {
  usePaintedSignal(requestId);
  const { remaining, fireOnce } = useCountdownTimer<Answer>(
    timeoutSecs,
    "timeout",
    onAnswer,
    100,
  );

  const progress = Math.max(0, Math.min(1, remaining / timeoutSecs));

  return (
    <HudCard>
      <HudCardBrandHeader />
      <div className="mt-3">
        <div className="text-sm font-semibold leading-tight">{title}</div>
        <div className="mt-1 text-[13px] leading-snug text-ink-muted">{body}</div>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={() => fireOnce("no")}
          className="hud-no-drag flex-1 rounded-lg border border-ink-border bg-white px-3 py-2 text-[13px] font-medium text-ink transition hover:bg-gray-50"
        >
          {noLabel}
        </button>
        <button
          type="button"
          onClick={() => fireOnce("yes")}
          className="hud-no-drag flex-1 rounded-lg bg-accent px-3 py-2 text-[13px] font-semibold text-white shadow transition hover:bg-accent-hover"
        >
          {yesLabel}
        </button>
      </div>
      <div className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-gray-200">
        <div
          className="h-full bg-accent transition-[width] duration-100 ease-linear"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
    </HudCard>
  );
}
