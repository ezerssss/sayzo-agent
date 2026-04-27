import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ExternalLink } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: () => void;
  onCancel: () => void;
}

type State = "idle" | "waiting" | "trusted";

const POLL_INTERVAL_MS = 1500;

export function Accessibility({ step, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (state !== "waiting") return;
    let cancelled = false;

    const tick = async () => {
      try {
        const { trusted } = await bridge.checkAccessibilityTrusted();
        if (cancelled) return;
        if (trusted) {
          setState("trusted");
          return;
        }
      } catch {
        // Bridge call failed (e.g., setup window torn down). Stop polling.
        return;
      }
      if (!cancelled) {
        pollRef.current = window.setTimeout(tick, POLL_INTERVAL_MS);
      }
    };

    pollRef.current = window.setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (pollRef.current !== null) {
        clearTimeout(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [state]);

  async function handleOpen() {
    try {
      await bridge.openAccessibilitySettings();
    } catch {
      // Best-effort — even if the deep-link spawn fails, the user can
      // navigate to System Settings manually. We still flip into the
      // waiting state so polling picks up the grant when it happens.
    }
    setState("waiting");
  }

  return (
    <Layout
      step={step}
      title="Accessibility access"
      subtitle="Lets your keyboard shortcut start Sayzo from any app, and helps Sayzo notice when you're in a web meeting (Meet, Zoom web, Teams web). Only the shortcut you picked wakes Sayzo up — anything else you type goes nowhere near it."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          {state === "trusted" ? (
            <Button onClick={onNext}>Continue</Button>
          ) : state === "waiting" ? (
            <Button variant="secondary" onClick={handleOpen}>
              <ExternalLink className="h-4 w-4" />
              Re-open Settings
            </Button>
          ) : (
            <Button onClick={handleOpen}>
              <ExternalLink className="h-4 w-4" />
              Open System Settings
            </Button>
          )}
        </>
      }
    >
      {state === "idle" && (
        <div className="space-y-3 text-sm leading-relaxed text-ink-muted">
          <p>
            <strong>What Sayzo can do with this:</strong> watch for the one
            shortcut you set, and read the title of your active browser tab
            (e.g.&nbsp;<em>"Meet — abc-defg-hij"</em>) to detect when you've
            joined a web meeting. Page contents, what you type into pages,
            your browsing history — none of it crosses over.
          </p>
          <p>
            Without this on, your shortcut won't work outside the Sayzo
            window and Sayzo can't auto-prompt for browser meetings — so
            we won't move you forward until it's set.
          </p>
        </div>
      )}

      {state === "waiting" && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium text-ink">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
            Waiting for Accessibility…
          </div>
          <div className="space-y-2 text-sm leading-relaxed text-ink-muted">
            <p>
              Sayzo isn't in the Accessibility list yet — macOS keeps that
              gate locked, so even Sayzo can't toggle it on for you. Add
              it once and we'll detect it automatically.
            </p>
            <ol className="list-decimal space-y-1 pl-5">
              <li>Click the <strong>+</strong> button under the list.</li>
              <li>
                Pick <strong>Sayzo</strong> from your Applications folder,
                then click Open.
              </li>
              <li>
                Toggle <strong>Sayzo</strong> on. We'll spot it within a
                second or two and unlock Continue.
              </li>
            </ol>
          </div>
        </div>
      )}

      {state === "trusted" && (
        <div className="flex items-center gap-2 text-sm font-medium text-green-700">
          <CheckCircle2 className="h-4 w-4" />
          All set! Your shortcut works anywhere, and Sayzo can spot
          browser meetings.
        </div>
      )}
    </Layout>
  );
}
