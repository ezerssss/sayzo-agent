import { cn } from "../../lib/cn";

interface Props {
  value: number; // 0..100; clamped
  indeterminate?: boolean;
  className?: string;
}

export function Progress({ value, indeterminate, className }: Props) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      className={cn(
        "relative h-1.5 w-full overflow-hidden rounded-full bg-ink-border",
        className,
      )}
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      {indeterminate ? (
        <div className="absolute inset-y-0 left-0 w-1/3 animate-pulse rounded-full bg-accent" />
      ) : (
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-150 ease-out"
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}
