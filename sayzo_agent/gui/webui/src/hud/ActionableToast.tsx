import { X } from "lucide-react";
import { useCountdownTimer } from "../lib/useCountdownTimer";
import { usePaintedSignal } from "../lib/usePaintedSignal";
import { HudCard, HudCardBrandHeader } from "./HudCard";

type Outcome = "pressed" | "expired" | "snoozed";

interface Props {
  requestId: string;
  title: string;
  body: string;
  buttonLabel: string;
  expireAfterSecs: number;
  onOutcome: (outcome: Outcome) => void;
  /** Optional "Snooze 1h" secondary button. Absent ⇒ single-button toast. */
  secondaryButtonLabel?: string;
}

export function ActionableToast({
  requestId,
  title,
  body,
  buttonLabel,
  expireAfterSecs,
  onOutcome,
  secondaryButtonLabel,
}: Props) {
  usePaintedSignal(requestId);
  const { remaining, fireOnce } = useCountdownTimer<Outcome>(
    expireAfterSecs,
    "expired",
    onOutcome,
    250,
  );

  const progress = Math.max(0, Math.min(1, remaining / expireAfterSecs));

  return (
    <HudCard>
      <HudCardBrandHeader />
      <button
        type="button"
        // Manual dismiss counts as the same outcome as the natural
        // timer expiry: the user didn't take the action. Same dispatch
        // shape so the parent doesn't need a third "dismissed" branch.
        onClick={() => fireOnce("expired")}
        title="Dismiss"
        aria-label="Dismiss"
        className="hud-no-drag absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full text-ink-muted transition hover:bg-gray-100 hover:text-ink"
      >
        <X size={13} strokeWidth={2.5} />
      </button>
      <div className="mt-3">
        <div className="text-sm font-semibold leading-tight">{title}</div>
        {body && (
          <div className="mt-1 text-[13px] leading-snug text-ink-muted">
            {body}
          </div>
        )}
      </div>
      <div className="mt-3 flex items-center gap-2">
        {secondaryButtonLabel && (
          <button
            type="button"
            // The user pressed the secondary action; the parent handles
            // it. Distinct outcome so the agent can record "saw it, chose
            // to defer" vs. a passive expiry.
            onClick={() => fireOnce("snoozed")}
            className="hud-no-drag rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-[13px] font-semibold text-ink-muted transition hover:bg-gray-100 hover:text-ink"
          >
            {secondaryButtonLabel}
          </button>
        )}
        <button
          type="button"
          onClick={() => fireOnce("pressed")}
          className="hud-no-drag rounded-lg bg-accent px-3 py-2 text-[13px] font-semibold text-white shadow transition hover:bg-accent-hover"
        >
          {buttonLabel}
        </button>
      </div>
      <div className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-gray-200">
        <div
          className="h-full bg-accent transition-[width] duration-200 ease-linear"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
    </HudCard>
  );
}
