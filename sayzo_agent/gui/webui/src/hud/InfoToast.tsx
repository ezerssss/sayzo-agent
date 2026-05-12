import { useEffect, useRef } from "react";
import { HudCard, HudCardBrandHeader } from "./HudCard";

interface Props {
  title: string;
  body: string;
  ttlSecs: number;
  onExpire: () => void;
}

export function InfoToast({ title, body, ttlSecs, onExpire }: Props) {
  const calledRef = useRef(false);
  useEffect(() => {
    const id = setTimeout(() => {
      if (calledRef.current) return;
      calledRef.current = true;
      onExpire();
    }, ttlSecs * 1000);
    return () => clearTimeout(id);
  }, [ttlSecs, onExpire]);

  return (
    <HudCard className="px-3 pb-3 pt-1">
      <HudCardBrandHeader size={24} textClassName="text-[15px]" />
      <div className="mt-2 flex gap-3">
        <div className="mt-1 h-2 w-2 shrink-0 rounded-full bg-accent" />
        <div className="flex-1">
          <div className="text-[13px] font-semibold leading-tight">{title}</div>
          {body && (
            <div className="mt-0.5 text-[12px] leading-snug text-ink-muted">
              {body}
            </div>
          )}
        </div>
      </div>
    </HudCard>
  );
}
