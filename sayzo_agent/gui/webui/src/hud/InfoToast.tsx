import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { usePaintedSignal } from "../lib/usePaintedSignal";
import { HudCard, HudCardBrandHeader } from "./HudCard";

interface Props {
  id: string;
  title: string;
  body: string;
  ttlSecs: number;
  onExpire: () => void;
}

export function InfoToast({ id, title, body, ttlSecs, onExpire }: Props) {
  // Single guard so the timer-driven and click-driven dismissal paths
  // can't both fire onExpire (the parent removes the toast on the first
  // call; a second would be a no-op anyway, but the guard keeps logs
  // clean).
  const calledRef = useRef(false);
  const dismiss = () => {
    if (calledRef.current) return;
    calledRef.current = true;
    onExpire();
  };

  usePaintedSignal(id);

  useEffect(() => {
    const timerId = setTimeout(dismiss, ttlSecs * 1000);
    return () => clearTimeout(timerId);
    // dismiss is captured at mount; we deliberately don't re-arm the
    // timer on parent re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ttlSecs]);

  return (
    <HudCard className="px-3 pb-3 pt-1">
      <HudCardBrandHeader size={24} textClassName="text-[15px]" />
      <button
        type="button"
        onClick={dismiss}
        title="Dismiss"
        aria-label="Dismiss"
        className="hud-no-drag absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full text-ink-muted transition hover:bg-gray-100 hover:text-ink"
      >
        <X size={13} strokeWidth={2.5} />
      </button>
      <div className="mt-2 flex items-start gap-3">
        <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-accent" />
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-semibold leading-tight">{title}</div>
          {body && (
            <div className="mt-0.5 text-[12px] leading-snug text-ink-muted">
              {body}
            </div>
          )}
        </div>
      </div>
      {/* Auto-dismiss countdown bar — the same kind of progress
          indicator the consent card and actionable toast already use.
          Tells the user "this will go away on its own" and how soon. */}
      <div className="mt-3 h-1 w-full overflow-hidden rounded-full bg-gray-100">
        <div
          className="h-full bg-accent"
          style={{
            // Bar starts at 100% width, animates down to 0% over ttl
            // via a single CSS transition. Driven by inline-styled
            // animationDuration to avoid bundling a dedicated keyframe
            // per ttlSecs value.
            animation: `hud-toast-countdown ${ttlSecs}s linear forwards`,
          }}
        />
      </div>
    </HudCard>
  );
}
