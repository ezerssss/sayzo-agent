import { Clock, X } from "lucide-react";
import { useCountdownTimer } from "../lib/useCountdownTimer";
import { usePaintedSignal } from "../lib/usePaintedSignal";
import { HudCard } from "./HudCard";

type Outcome = "pressed" | "expired" | "snoozed";

interface Props {
  requestId: string;
  /** Plain, self-explanatory headline (server-generated). */
  headline: string;
  /** The concrete suggestion / rewrite / observation. */
  body: string;
  /** "From your {source}" context anchor — agent-supplied from the capture title. */
  sourceLabel: string;
  /** Chip text — "Just now" / "5 min ago" / "1 hr ago". Computed agent-side at fire time. */
  freshnessLabel: string;
  /** Verbatim quote from the user's own speech. Absent for non-utterance insight types. */
  quote?: string;
  /** Primary button — "See full feedback" (opens the deep-link). */
  buttonLabel: string;
  /** Secondary "Stop showing these" off-switch. Absent ⇒ single-button card. */
  secondaryButtonLabel?: string;
  expireAfterSecs: number;
  onOutcome: (outcome: Outcome) => void;
}

// Compact post-capture coaching card (v3.10+). Deliberately lighter than a
// "rich" card: one source line + headline + at most one short quote + one
// line of advice. The "why it helps" lives behind "See full feedback", not
// here — the card has to be absorbable at a glance, not be a reading task.
// Countdown + single-fire latch via useCountdownTimer — same hook
// ActionableToast uses, so auto-expire / dismiss / press / stop semantics
// stay identical across HUD cards.
export function InsightCard({
  requestId,
  headline,
  body,
  sourceLabel,
  freshnessLabel,
  quote,
  buttonLabel,
  secondaryButtonLabel,
  expireAfterSecs,
  onOutcome,
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
    <HudCard className="px-4 pb-4 pt-1">
      <button
        type="button"
        // Manual dismiss == natural expiry: the user saw it and moved on.
        onClick={() => fireOnce("expired")}
        title="Dismiss"
        aria-label="Dismiss"
        className="hud-no-drag absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full text-ink-muted transition hover:bg-gray-100 hover:text-ink"
      >
        <X size={13} strokeWidth={2.5} />
      </button>

      {/* Source anchor — chip + sentence reads as a thread-reply
          breadcrumb (timestamp + source), not a category tag. The bold
          source label is the visual anchor ("ah, that's the call I just
          had"); the accent-tinted freshness chip ties the row to the
          primary CTA's color so the card reads as one composition.
          Wraps to two lines for long server titles (no truncate —
          losing the most-anchoring word defeats the point). */}
      <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 pr-6 text-[12px] leading-snug text-ink-muted">
        <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-accent/10 px-1.5 py-0.5 text-[10.5px] font-semibold text-accent">
          <Clock size={10} strokeWidth={2.5} />
          {freshnessLabel}
        </span>
        <span>
          from your{" "}
          <span className="font-semibold text-ink">{sourceLabel}</span>
        </span>
      </div>

      <div className="mt-2">
        <div className="text-sm font-semibold leading-tight text-ink">
          {headline}
        </div>

        {quote && (
          <div className="mt-2 border-l-2 border-gray-200 pl-2.5 text-[13px] italic leading-snug text-ink-muted">
            “{quote}”
          </div>
        )}

        {body && (
          <div className="mt-2 text-[13px] leading-snug text-ink-muted">
            {body}
          </div>
        )}
      </div>

      <div className="mt-3 flex items-center gap-2">
        {secondaryButtonLabel && (
          <button
            type="button"
            // "Stop showing these" — reuses the "snoozed" wire outcome,
            // which the launcher routes to the secondary callback (the
            // off-switch). Distinct from "expired" so the agent knows
            // this was a deliberate opt-out.
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
