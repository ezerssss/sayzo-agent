import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { HudCard, HudCardBrandHeader } from "./HudCard";

interface Props {
  title: string;
  body: string;
  buttonLabel: string;
  expireAfterSecs: number;
  onOutcome: (outcome: "pressed" | "expired" | "snoozed") => void;
  /** Optional "Snooze 1h" secondary button. Absent ⇒ single-button toast. */
  secondaryButtonLabel?: string;
}

export function ActionableToast({
  title,
  body,
  buttonLabel,
  expireAfterSecs,
  onOutcome,
  secondaryButtonLabel,
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
    // Manual dismiss counts as the same outcome as the natural
    // timer expiry: the user didn't take the action. Same dispatch
    // shape so the parent doesn't need a third "dismissed" branch.
    if (calledRef.current) return;
    calledRef.current = true;
    onOutcome("expired");
  }

  function handleSnooze() {
    // Same single-fire latch as press / dismiss — the user deferred
    // the drill; the parent re-fires it later. Distinct outcome so the
    // agent can record it as a "saw it, chose to wait" signal.
    if (calledRef.current) return;
    calledRef.current = true;
    onOutcome("snoozed");
  }

  const progress = Math.max(0, Math.min(1, remaining / expireAfterSecs));

  return (
    <HudCard>
      <HudCardBrandHeader />
      <button
        type="button"
        onClick={handleDismiss}
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
            onClick={handleSnooze}
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
