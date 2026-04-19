import { ButtonHTMLAttributes, forwardRef } from "react";
import { cn } from "../../lib/cn";

type Variant = "primary" | "secondary" | "ghost";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}

const variantClasses: Record<Variant, string> = {
  primary:
    "bg-accent text-white hover:bg-accent-hover focus-visible:ring-accent-ring",
  secondary:
    "bg-white text-ink border border-ink-border hover:bg-gray-50 focus-visible:ring-ink-border",
  ghost:
    "bg-transparent text-ink-muted hover:text-ink focus-visible:ring-ink-border",
};

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = "primary", className, ...rest }: Props,
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
        "disabled:opacity-50 disabled:pointer-events-none",
        variantClasses[variant],
        className,
      )}
      {...rest}
    />
  );
});
