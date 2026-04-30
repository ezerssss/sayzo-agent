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

// Canonical modifier render order — mirrors what Windows / macOS / VS Code
// / most apps use. We display in this order regardless of the order the
// user pressed the modifiers, so two equivalent combos (Ctrl+Alt+A vs
// Alt+Ctrl+A) render and serialise identically.
const MODIFIER_ORDER = ["ctrl", "alt", "shift", "cmd"] as const;

function humanizeBinding(binding: string): string {
  return binding
    .split("+")
    .map((part) => (part.length === 1 ? part.toUpperCase() : titleCase(part)))
    .join("+");
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Translate ``KeyboardEvent.code`` (the *physical* key, layout-agnostic)
// into the lowercase token our backend's hotkey grammar accepts (see
// ``arm/hotkey_mac.py::_VK`` and pynput's grammar).
//
// On macOS, pressing Alt+M produces ``event.key === "µ"`` because the OS
// resolves Option-modified characters before the JS event fires. Using
// ``event.code`` (``"KeyM"``) instead lets us recover the underlying
// letter so combos like Alt+M and Alt+A actually save as ``alt+m`` /
// ``alt+a`` rather than ``alt+µ`` / ``alt+å`` (which the agent's parser
// rejects as unsupported keys).
function physicalKeyFromCode(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.charAt(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.charAt(5);
  if (/^F([1-9]|1[0-2])$/.test(code)) return code.toLowerCase();
  switch (code) {
    case "Space":
      return "space";
    case "Enter":
    case "NumpadEnter":
      return "enter";
    case "Tab":
      return "tab";
    case "Backspace":
      return "backspace";
    case "Delete":
      return "delete";
    case "ArrowUp":
      return "up";
    case "ArrowDown":
      return "down";
    case "ArrowLeft":
      return "left";
    case "ArrowRight":
      return "right";
    case "Minus":
      return "-";
    case "Equal":
      return "=";
    case "BracketLeft":
      return "[";
    case "BracketRight":
      return "]";
    case "Backslash":
      return "\\";
    case "Semicolon":
      return ";";
    case "Quote":
      return "'";
    case "Comma":
      return ",";
    case "Period":
      return ".";
    case "Slash":
      return "/";
    case "Backquote":
      return "`";
    default:
      return null;
  }
}

// Sort the held modifiers by canonical render order. Anything outside
// the canonical set (shouldn't happen, but defensive) is appended at
// the end in insertion order.
function sortModifiers(mods: Iterable<string>): string[] {
  const set = new Set(mods);
  const ordered: string[] = [];
  for (const m of MODIFIER_ORDER) {
    if (set.has(m)) {
      ordered.push(m);
      set.delete(m);
    }
  }
  ordered.push(...set);
  return ordered;
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

      const key = physicalKeyFromCode(e.code);
      if (key === null) {
        setStatus({
          text: "That key isn't supported. Try a letter, digit, function key, or arrow.",
          tone: "error",
        });
        return;
      }
      // Render modifiers in canonical order (Ctrl → Alt → Shift → Cmd)
      // so two equivalent combos serialise the same way regardless of
      // which modifier the user pressed first.
      const mods = sortModifiers(heldModifiersRef.current);
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
