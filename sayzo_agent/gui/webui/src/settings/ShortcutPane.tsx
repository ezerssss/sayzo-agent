import { useEffect, useState } from "react";
import { settingsBridge } from "../lib/settings-bridge";
import { ShortcutCapture } from "../components/ShortcutCapture";

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; display: string }
  | { kind: "error"; message: string };

export function ShortcutPane() {
  const [binding, setBinding] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const h = await settingsBridge.getHotkey();
        if (!cancelled) setBinding(h.binding);
      } catch (e) {
        // Don't fall back to the default binding here — that would silently
        // misrepresent the user's actual saved shortcut. Surface the error
        // so they can retry or check the agent log instead.
        if (!cancelled) setLoadError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleChange(next: string) {
    setSave({ kind: "saving" });
    try {
      const result = await settingsBridge.saveHotkey(next);
      if (result.error !== null) {
        setSave({ kind: "error", message: result.error });
        return;
      }
      setBinding(next);
      setSave({ kind: "saved", display: result.display ?? next });
      window.setTimeout(() => {
        setSave((cur) => (cur.kind === "saved" ? { kind: "idle" } : cur));
      }, 2500);
    } catch (e) {
      setSave({ kind: "error", message: String(e) });
    }
  }

  if (loadError != null) {
    return (
      <div className="text-sm text-red-700">
        Couldn't load your shortcut. {loadError}
      </div>
    );
  }
  if (binding == null) {
    return <div className="text-sm text-ink-muted">Loading shortcut…</div>;
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Shortcut
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Press the shortcut anywhere on your computer to start a Sayzo
        capture. Tap it again to stop.
      </p>

      <div className="mt-8">
        <ShortcutCapture initialBinding={binding} onChange={handleChange} />
      </div>

      <div className="mt-3 min-h-[1.25rem] text-xs">
        {save.kind === "saving" && (
          <span className="text-ink-muted">Saving…</span>
        )}
        {save.kind === "saved" && (
          <span className="text-ink-muted">
            ✓ Saved — {save.display} is now your Sayzo shortcut.
          </span>
        )}
        {save.kind === "error" && (
          <span className="text-red-600">{save.message}</span>
        )}
      </div>
    </div>
  );
}
