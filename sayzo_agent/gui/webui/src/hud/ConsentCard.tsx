import { useEffect, useRef, useState } from "react";
import { HudCard, HudCardBrandHeader } from "./HudCard";

interface Props {
  title: string;
  body: string;
  yesLabel: string;
  noLabel: string;
  timeoutSecs: number;
  onAnswer: (answer: "yes" | "no" | "timeout") => void;
}

export function ConsentCard({
  title,
  body,
  yesLabel,
  noLabel,
  timeoutSecs,
  onAnswer,
}: Props) {
  const [remaining, setRemaining] = useState(timeoutSecs);
  const answeredRef = useRef(false);

  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      const left = Math.max(0, timeoutSecs - elapsed);
      setRemaining(left);
      if (left <= 0 && !answeredRef.current) {
        answeredRef.current = true;
        clearInterval(id);
        onAnswer("timeout");
      }
    }, 100);
    return () => clearInterval(id);
  }, [timeoutSecs, onAnswer]);

  function handle(answer: "yes" | "no") {
    if (answeredRef.current) return;
    answeredRef.current = true;
    onAnswer(answer);
  }

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
          onClick={() => handle("no")}
          className="hud-no-drag flex-1 rounded-lg border border-ink-border bg-white px-3 py-2 text-[13px] font-medium text-ink transition hover:bg-gray-50"
        >
          {noLabel}
        </button>
        <button
          type="button"
          onClick={() => handle("yes")}
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
