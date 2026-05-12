import { useEffect, useRef, useState } from "react";
import { HudCard, HudCardBrandHeader } from "./HudCard";

interface Props {
  title: string;
  body: string;
  buttonLabel: string;
  expireAfterSecs: number;
  onOutcome: (outcome: "pressed" | "expired") => void;
}

export function ActionableToast({
  title,
  body,
  buttonLabel,
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

  const progress = Math.max(0, Math.min(1, remaining / expireAfterSecs));

  return (
    <HudCard>
      <HudCardBrandHeader />
      <div className="mt-3">
        <div className="text-sm font-semibold leading-tight">{title}</div>
        {body && (
          <div className="mt-1 text-[13px] leading-snug text-ink-muted">
            {body}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={handlePress}
        className="hud-no-drag mt-3 rounded-lg bg-accent px-3 py-2 text-[13px] font-semibold text-white shadow transition hover:bg-accent-hover"
      >
        {buttonLabel}
      </button>
      <div className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-gray-200">
        <div
          className="h-full bg-accent transition-[width] duration-200 ease-linear"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
    </HudCard>
  );
}
