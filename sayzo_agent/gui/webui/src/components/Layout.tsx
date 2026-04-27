import { ReactNode } from "react";

// Single-column setup window chrome. Logo + step number top-left, content
// below. Matches the marketing site's "minimal, structure-first" feel.
interface Props {
  step?: string;        // "01" / "02" — small numeric step indicator
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;   // CTA row at bottom
}

export function Layout({ step, title, subtitle, children, footer }: Props) {
  return (
    <div className="flex min-h-full flex-col px-10 py-12">
      <header className="mb-12 flex items-center gap-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-ink text-xs font-semibold text-white">
          S
        </div>
        <span className="text-sm font-medium tracking-tight text-ink">
          Sayzo
        </span>
      </header>

      <div className="mx-auto w-full max-w-md flex-1">
        {step && (
          <div className="mb-4 flex items-baseline gap-3 text-ink-muted">
            <span className="text-xs font-medium tracking-wider">{step}</span>
            <span className="h-px flex-1 bg-ink-border" />
          </div>
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-2 text-sm leading-relaxed text-ink-muted">
            {subtitle}
          </p>
        )}
        <div className="mt-8">{children}</div>
      </div>

      {footer && (
        <div className="mx-auto mt-auto flex w-full max-w-md items-center justify-end gap-3 pt-8">
          {footer}
        </div>
      )}
    </div>
  );
}
