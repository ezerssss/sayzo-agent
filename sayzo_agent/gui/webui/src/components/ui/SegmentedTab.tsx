import { cn } from "../../lib/cn";

interface Props {
  label: string;
  selected: boolean;
  onClick: () => void;
}

// Pill-shaped segmented-control button used in the Meeting Apps pane (top
// section toggle) and the Add-app dialog (Desktop / Web tab toggle). One
// component so the visual language stays consistent across the two
// surfaces and a future restyle only touches one place.
export function SegmentedTab({ label, selected, onClick }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md px-4 py-2 text-sm font-medium transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
        selected
          ? "bg-accent text-white focus-visible:ring-accent-ring"
          : "bg-white text-ink border border-ink-border hover:bg-gray-50 focus-visible:ring-ink-border",
      )}
    >
      {label}
    </button>
  );
}
