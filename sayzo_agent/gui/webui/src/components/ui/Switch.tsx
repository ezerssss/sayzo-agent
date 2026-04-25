import { cn } from "../../lib/cn";

interface Props {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  // Optional accessible label. If not provided, the caller should associate
  // a label via `aria-labelledby` or wrap the Switch in a clickable label.
  ariaLabel?: string;
}

// Minimal pill-shaped toggle. Used by Settings panes that need on/off
// switches (Notifications, Meeting Apps detector toggles). Visuals match
// the rest of the Sayzo UI: white surface, accent blue when on, slate when
// off, ink-border when off, ring on focus. Click target is the whole pill.
export function Switch({ checked, onChange, disabled, ariaLabel }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-accent-ring",
        "disabled:opacity-50 disabled:pointer-events-none",
        checked ? "bg-accent" : "bg-gray-300",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        )}
      />
    </button>
  );
}
