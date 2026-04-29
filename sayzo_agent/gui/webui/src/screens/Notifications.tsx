import { useEffect, useRef, useState } from "react";
import { Bell, CheckCircle2, ExternalLink } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  platform: string;
  onNext: () => void;
  onCancel: () => void;
}

// idle    — not yet attempted
// asking  — prompt_notification_permission() in flight
// waiting — first attempt didn't grant; we've deep-linked the user into
//           System Settings and are polling for them to flip the toggle.
//           Mirrors the Accessibility screen's polling pattern.
// granted — confirmed authorized; verification toast fired
type State = "idle" | "asking" | "waiting" | "granted";

const POLL_INTERVAL_MS = 1500;
// macOS in particular sometimes doesn't refresh the auth bit for an
// already-running process even after the user toggles us on; this hint
// surfaces the same restart escape hatch the Accessibility screen offers,
// 10s into waiting state.
const RESTART_HINT_DELAY_MS = 10_000;

export function Notifications({ step, platform, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");
  const [showRestartHint, setShowRestartHint] = useState(false);
  const [showSkipConfirm, setShowSkipConfirm] = useState(false);
  const pollRef = useRef<number | null>(null);
  const isMac = platform === "darwin";

  useEffect(() => {
    if (state !== "waiting") {
      setShowRestartHint(false);
      return;
    }
    let cancelled = false;

    const tick = async () => {
      try {
        const { granted } = await bridge.checkNotificationPermission();
        if (cancelled) return;
        if (granted === true) {
          setState("granted");
          void bridge.sendTestNotification();
          return;
        }
      } catch {
        // Bridge call failed (window torn down, backend died) — stop polling.
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

  async function handleAllow() {
    setState("asking");
    try {
      const { granted } = await bridge.promptNotificationPermission();
      if (granted === true) {
        setState("granted");
        void bridge.sendTestNotification();
      } else {
        // False (denied) or null (inconclusive — common on Windows where
        // there's no real grant flow, just a current-state probe). Either
        // way, auto-deep-link to Settings so the user sees an obvious
        // next action, and start polling for the toggle to flip.
        setState("waiting");
        void bridge.openNotificationSettings();
      }
    } catch {
      setState("idle");
    }
  }

  async function handleReopenSettings() {
    try {
      await bridge.openNotificationSettings();
    } catch {
      // Best-effort.
    }
  }

  return (
    <Layout
      step={step}
      title="Turn on notifications"
      subtitle="Sayzo uses notifications to ask before recording in your meetings, and to confirm when a capture is saved. This step is essential — without notifications, Sayzo can't ask for your consent."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          {state === "granted" ? (
            <Button onClick={onNext}>Continue</Button>
          ) : state === "waiting" ? (
            <Button variant="secondary" onClick={handleReopenSettings}>
              <ExternalLink className="h-4 w-4" />
              Re-open Settings
            </Button>
          ) : (
            <Button onClick={handleAllow} disabled={state === "asking"}>
              {state === "asking" ? "Asking…" : "Allow"}
            </Button>
          )}
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border border-ink-border p-4">
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-gray-100 text-ink">
          <Bell className="h-4 w-4" />
        </div>
        <div className="flex-1 space-y-2">
          <h3 className="text-sm font-medium text-ink">Notifications</h3>

          {state === "idle" && (
            <p className="text-sm leading-relaxed text-ink-muted">
              Click Allow to start.{" "}
              {isMac
                ? "macOS will show a system prompt — click Allow there too."
                : "Sayzo will check whether Windows is set up to display its toasts."}
            </p>
          )}

          {state === "asking" && (
            <div className="flex items-center gap-2 text-sm font-medium text-ink">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
              Asking…
            </div>
          )}

          {state === "waiting" && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-medium text-ink">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
                Waiting for notifications…
              </div>
              <p className="text-sm leading-relaxed text-ink-muted">
                {isMac ? (
                  <>
                    Open <strong>System Settings → Notifications → Sayzo</strong>{" "}
                    and turn it on. We'll detect it within a second or two.
                  </>
                ) : (
                  <>
                    Open <strong>Settings → System → Notifications → Sayzo</strong>{" "}
                    and turn it on. We'll detect it within a second or two.
                  </>
                )}
              </p>
              {showRestartHint && (
                <div className="rounded-md border border-ink-muted/20 bg-ink-muted/5 p-3 text-sm leading-relaxed">
                  <p className="font-medium text-ink">
                    Already turned it on?
                  </p>
                  <p className="mt-1 text-ink-muted">
                    Sayzo sometimes doesn't pick up the change until the next
                    launch. Try toggling it off and on, or restart Sayzo.
                  </p>
                </div>
              )}
            </div>
          )}

          {state === "granted" && (
            <div className="space-y-1">
              <p className="flex items-center gap-1 text-sm font-medium text-green-700">
                <CheckCircle2 className="h-4 w-4" />
                All set!
              </p>
              <p className="text-sm leading-relaxed text-ink-muted">
                You should see a test notification — that's the kind of nudge
                Sayzo will use during meetings.
              </p>
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 flex justify-center">
        <button
          type="button"
          onClick={() => setShowSkipConfirm(true)}
          className="text-xs text-ink-muted underline-offset-2 hover:text-ink hover:underline"
        >
          I'll set this up later
        </button>
      </div>

      {showSkipConfirm && (
        <SkipConfirmDialog
          onSkip={() => {
            setShowSkipConfirm(false);
            onNext();
          }}
          onStay={() => setShowSkipConfirm(false)}
        />
      )}
    </Layout>
  );
}

function SkipConfirmDialog({
  onSkip,
  onStay,
}: {
  onSkip: () => void;
  onStay: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-6">
      <div className="w-full max-w-sm rounded-md border border-ink-border bg-white p-6 shadow-lg">
        <h2 className="text-base font-semibold text-ink">
          Skip notifications?
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          Without notifications, Sayzo can't ask before joining your meetings,
          and the global shortcut won't show a confirmation. You can turn this
          on later from Settings.
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="ghost" onClick={onSkip}>
            Skip anyway
          </Button>
          <Button onClick={onStay}>Stay and set up</Button>
        </div>
      </div>
    </div>
  );
}
