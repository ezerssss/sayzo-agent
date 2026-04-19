import { ReactNode } from "react";
import { AlertCircle } from "lucide-react";
import { cn } from "../../lib/cn";

interface Props {
  variant?: "destructive";
  children: ReactNode;
  className?: string;
}

export function Alert({ variant = "destructive", children, className }: Props) {
  return (
    <div
      role="alert"
      className={cn(
        "flex gap-3 rounded-md border p-3 text-sm",
        variant === "destructive" &&
          "border-red-200 bg-red-50 text-red-900",
        className,
      )}
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <div>{children}</div>
    </div>
  );
}
