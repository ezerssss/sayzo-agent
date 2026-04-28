import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ExternalLink, RefreshCw } from "lucide-react";
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
// macOS doesn't always notify a running process when its Accessibility
// entry is granted — the trust bit can stay False even after a real grant
// until the app is relaunched. After this many ms in "waiting", we surface
// a Restart button so the user is never stuck.
const RESTART_HINT_DELAY_MS = 10_000;

export function Accessibility({ step, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");
  const [showRestartHint, setShowRestartHint] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (state !== "waiting") {
      setShowRestartHint(false);
      return;
    }
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
    const hintTimer = window.setTimeout(
      () => !cancelled && setShowRestartHint(true),
      RESTART_HINT_DELAY_MS,
    );
    return () => {
      cancelled = true;
      if (pollRef.current !== null) {
        clearTimeout(pollRef.current);
        pollRef.current = null;
      }
      clearTimeout(hintTimer);
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

  async function handleRestart() {
    try {
      await bridge.restartApp();
    } catch {
      // restart_app hard-exits on the Python side, so the bridge call
      // typically never resolves. The catch is just defensive.
    }
  }

  return (
    <Layout
      step={step}
      title="Accessibility access"
      subtitle="Lets your keyboard shortcut start Sayzo from any app."
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
            Enable Accessibility access so Sayzo can respond to shortcuts
            anywhere on your Mac.
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

          {showRestartHint && (
            <div className="rounded-md border border-ink-muted/20 bg-ink-muted/5 p-3 text-sm leading-relaxed">
              <p className="font-medium text-ink">
                Already added Sayzo and toggled it on?
              </p>
              <p className="mt-1 text-ink-muted">
                macOS sometimes doesn't tell a running app that its
                Accessibility was just granted. A quick restart picks it
                up — your progress so far is saved.
              </p>
              <Button
                variant="secondary"
                onClick={handleRestart}
                className="mt-2"
              >
                <RefreshCw className="h-4 w-4" />
                Restart Sayzo
              </Button>
            </div>
          )}
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
