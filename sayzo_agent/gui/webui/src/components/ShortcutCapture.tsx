import { useEffect, useRef, useState } from "react";
import { Button } from "./ui/Button";
import { bridge } from "../lib/bridge";
import { cn } from "../lib/cn";

// React mirror of the tkinter ShortcutCaptureField. Click "Change…", press
// a key combination with at least one modifier, the new binding appears
// in the pill. Esc cancels. Validation runs through the Python bridge so
// the error strings match what the Settings window would show.

interface Props {
  initialBinding: string;
  onChange: (binding: string) => void;
}

// Keyboard `event.key` values for modifier keys, mapped to the canonical
// names used in our stored binding format (see arm/hotkey.py).
const MODIFIER_KEYS: Record<string, string> = {
  Control: "ctrl",
  Alt: "alt",
  AltGraph: "alt",
  Shift: "shift",
  Meta: "cmd",
  OS: "cmd",
};

function humanizeBinding(binding: string): string {
  return binding
    .split("+")
    .map((part) => (part.length === 1 ? part.toUpperCase() : titleCase(part)))
    .join("+");
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Normalize `event.key` for the main (non-modifier) key. Lowercase single
// chars, keep named keys like "F5" / "Space" / "Enter" readable.
function normalizeKey(key: string): string {
  if (key === " ") return "space";
  if (key.length === 1) return key.toLowerCase();
  // Arrow keys, function keys, etc. tkinter keysyms are lowercase in our
  // storage format, so match that.
  return key.toLowerCase();
}

export function ShortcutCapture({ initialBinding, onChange }: Props) {
  const [binding, setBinding] = useState(initialBinding);
  const [capturing, setCapturing] = useState(false);
  const [status, setStatus] = useState<{
    text: string;
    tone: "muted" | "error";
  }>({ text: "", tone: "muted" });

  const heldModifiersRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!capturing) return;

    function onKeyDown(e: KeyboardEvent) {
      e.preventDefault();
      e.stopPropagation();

      if (e.key === "Escape") {
        heldModifiersRef.current.clear();
        setCapturing(false);
        setStatus({ text: "", tone: "muted" });
        return;
      }

      const mod = MODIFIER_KEYS[e.key];
      if (mod !== undefined) {
        heldModifiersRef.current.add(mod);
        return;
      }

      if (heldModifiersRef.current.size === 0) {
        setStatus({
          text: "Please include at least one modifier (Ctrl, Alt, Shift, ⌘).",
          tone: "error",
        });
        return;
      }

      const key = normalizeKey(e.key);
      // Sort modifiers so equivalent combos stringify the same way.
      const mods = Array.from(heldModifiersRef.current).sort();
      const candidate = [...mods, key].join("+");

      // Bounce through the bridge for validation so the error strings are
      // exactly the ones the tkinter widget used to show.
      void bridge.validateHotkey(candidate).then((v) => {
        if (v.error !== null) {
          setStatus({ text: v.error, tone: "error" });
          return;
        }
        heldModifiersRef.current.clear();
        setBinding(candidate);
        setCapturing(false);
        setStatus({ text: "", tone: "muted" });
        onChange(candidate);
      });
    }

    function onKeyUp(e: KeyboardEvent) {
      const mod = MODIFIER_KEYS[e.key];
      if (mod !== undefined) {
        heldModifiersRef.current.delete(mod);
      }
    }

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
    };
  }, [capturing, onChange]);

  function startCapture() {
    heldModifiersRef.current.clear();
    setCapturing(true);
    setStatus({
      text: "Press a key combination… (Esc to cancel)",
      tone: "muted",
    });
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        <div
          className={cn(
            "inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-semibold",
            capturing
              ? "border-accent bg-white text-ink"
              : "border-ink-border bg-white text-ink",
          )}
        >
          {humanizeBinding(binding)}
        </div>
        <Button
          variant="secondary"
          onClick={startCapture}
          disabled={capturing}
        >
          {capturing ? "Recording…" : "Change…"}
        </Button>
      </div>
      {status.text && (
        <p
          className={cn(
            "text-xs leading-relaxed",
            status.tone === "error" ? "text-red-600" : "text-ink-muted",
          )}
        >
          {status.text}
        </p>
      )}
    </div>
  );
}
