import { useEffect, useRef, useState } from "react";
import { Bell, ExternalLink } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

// Windows notifications flow — lighter than the macOS Permissions screen
// because Windows doesn't surface a blocking permission dialog for toasts.
// We check the current state on mount; if already enabled we advance
// silently. If blocked (Focus Assist or per-app toggle), we show a
// "Open Settings" CTA and a recheck button.
export function NotificationsWin({ onDone, onCancel }: Props) {
  const [status, setStatus] = useState<"checking" | "ok" | "blocked" | "error">(
    "checking",
  );
  const [hint, setHint] = useState<string | null>(null);
  // The initial check sometimes resolves very quickly — avoid flashing the
  // "blocked" UI if we're actually about to advance.
  const didInitialCheck = useRef(false);

  async function check(): Promise<void> {
    try {
      const { granted } = await bridge.promptNotificationPermission();
      if (granted === true) {
        setStatus("ok");
        // Give React a tick to flush state, then advance.
        setTimeout(onDone, 100);
      } else if (granted === false) {
        setStatus("blocked");
      } else {
        // null — couldn't determine. Treat as blocked so the user has a
        // chance to check Settings themselves.
        setStatus("blocked");
        setHint(
          "Couldn't read the current notification setting. If toasts don't show up after setup, open Settings and make sure Sayzo Agent is allowed.",
        );
      }
    } catch (e) {
      setStatus("error");
      setHint(String(e));
    }
  }

  useEffect(() => {
    if (didInitialCheck.current) return;
    didInitialCheck.current = true;
    void check();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (status === "checking" || status === "ok") {
    return (
      <Layout
        step="03"
        title="Checking notifications"
        subtitle="One moment while we confirm Windows will show Sayzo's conversation toasts."
      >
        <div className="flex items-center gap-3 text-ink-muted">
          <div className="h-2 w-2 animate-pulse rounded-full bg-accent" />
          <span className="text-sm">Checking current setting…</span>
        </div>
      </Layout>
    );
  }

  return (
    <Layout
      step="03"
      title="Enable notifications"
      subtitle="Sayzo shows a short toast each time a conversation is saved. Windows is currently blocking Sayzo's toasts — enable them in Settings, then come back."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Skip
          </Button>
          <Button onClick={() => void check()}>I've enabled them</Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="flex items-start gap-3 rounded-md border border-ink-border p-4">
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-gray-100 text-ink">
            <Bell className="h-4 w-4" />
          </div>
          <div className="flex-1 space-y-1">
            <h3 className="text-sm font-medium text-ink">Notifications</h3>
            <p className="text-sm leading-relaxed text-ink-muted">
              Enable &ldquo;Sayzo Agent&rdquo; under Settings → System →
              Notifications. Focus Assist also needs to allow it.
            </p>
          </div>
        </div>

        <Button
          variant="secondary"
          onClick={() => bridge.openNotificationSettings()}
        >
          <ExternalLink className="h-4 w-4" />
          Open notification settings
        </Button>

        {hint && (
          <Alert>
            <span>{hint}</span>
          </Alert>
        )}
      </div>
    </Layout>
  );
}
