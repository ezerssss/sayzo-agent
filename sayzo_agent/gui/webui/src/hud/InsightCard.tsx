import { useEffect, useRef, useState } from "react";
import { Sparkles, X } from "lucide-react";
import { HudCard } from "./HudCard";

interface Props {
  /** Plain, self-explanatory headline (server-generated). */
  headline: string;
  /** The concrete suggestion / rewrite / observation. */
  body: string;
  /** "From your {source}" context anchor — agent-supplied from the capture title. */
  sourceLabel: string;
  /** Verbatim quote from the user's own speech. Absent for non-utterance insight types. */
  quote?: string;
  /** Primary button — "See full feedback" (opens the deep-link). */
  buttonLabel: string;
  /** Secondary "Stop showing these" off-switch. Absent ⇒ single-button card. */
  secondaryButtonLabel?: string;
  expireAfterSecs: number;
  onOutcome: (outcome: "pressed" | "expired" | "snoozed") => void;
}

// Compact post-capture coaching card (v3.10+). Deliberately lighter than a
// "rich" card: one source line + headline + at most one short quote + one
// line of advice. The "why it helps" lives behind "See full feedback", not
// here — the card has to be absorbable at a glance, not be a reading task.
// Countdown + single-fire latch mirror ActionableToast so the auto-expire /
// dismiss / press / stop semantics stay identical across HUD cards.
export function InsightCard({
  headline,
  body,
  sourceLabel,
  quote,
  buttonLabel,
  secondaryButtonLabel,
  expireAfterSecs,
  onOutcome,
}: Props) {
  const [remaining, setRemaining] = useState(expireAfterSecs);
  const calledRef = useRef(false);

  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      const left = Math.max(0, expireAfterSecs - elapsed);
      setRemaining(left);
      if (left <= 0 && !calledRef.current) {
        calledRef.current = true;
        clearInterval(id);
        onOutcome("expired");
      }
    }, 250);
    return () => clearInterval(id);
  }, [expireAfterSecs, onOutcome]);

  function handlePress() {
    if (calledRef.current) return;
    calledRef.current = true;
    onOutcome("pressed");
  }

  function handleDismiss() {
    // Manual dismiss == natural expiry: the user saw it and moved on.
    if (calledRef.current) return;
    calledRef.current = true;
    onOutcome("expired");
  }

  function handleStop() {
    // "Stop showing these" — reuses the "snoozed" wire outcome, which the
    // launcher routes to the secondary callback (the off-switch). Distinct
    // from "expired" so the agent knows this was a deliberate opt-out.
    if (calledRef.current) return;
    calledRef.current = true;
    onOutcome("snoozed");
  }

  const progress = Math.max(0, Math.min(1, remaining / expireAfterSecs));

  return (
    <HudCard className="px-4 pb-4 pt-1">
      <button
        type="button"
        onClick={handleDismiss}
        title="Dismiss"
        aria-label="Dismiss"
        className="hud-no-drag absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full text-ink-muted transition hover:bg-gray-100 hover:text-ink"
      >
        <X size={13} strokeWidth={2.5} />
      </button>

      {/* Source anchor — tells the user where this came from at a glance. */}
      <div className="mt-2 flex items-center gap-1.5 pr-6 text-[11px] font-medium uppercase tracking-wide text-ink-muted">
        <Sparkles size={12} className="shrink-0 text-accent" />
        <span className="truncate">From your {sourceLabel}</span>
      </div>

      <div className="mt-1.5">
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
            onClick={handleStop}
            className="hud-no-drag rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-[13px] font-semibold text-ink-muted transition hover:bg-gray-100 hover:text-ink"
          >
            {secondaryButtonLabel}
          </button>
        )}
        <button
          type="button"
          onClick={handlePress}
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
