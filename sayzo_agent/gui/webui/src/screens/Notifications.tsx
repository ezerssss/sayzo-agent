import { useState } from "react";
import { Bell, CheckCircle2, ExternalLink, XCircle } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  platform: string;
  onNext: () => void;
  onCancel: () => void;
}

type State = "idle" | "pending" | "granted" | "denied";

// Platform-specific copy: macOS surfaces a real OS prompt the first time
// we call request_authorisation; Windows doesn't (Focus Assist + the
// per-app toggle are both settings the user manages themselves), so we
// frame it as a best-effort check instead of a grant.

export function Notifications({ step, platform, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");

  async function handleGrant() {
    setState("pending");
    try {
      const { granted } = await bridge.promptNotificationPermission();
      setState(granted === true ? "granted" : granted === false ? "denied" : "idle");
    } catch {
      setState("idle");
    }
  }

  const isMac = platform === "darwin";
  const title = isMac
    ? "Let Sayzo send you notifications"
    : "Check your notification settings";
  const subtitle = isMac
    ? "Sayzo asks before recording when it spots you in a meeting, and lets you know when a conversation saves. Skip this and you won't see the ask."
    : "Sayzo asks before recording when it spots you in a meeting. Make sure notifications are on so the prompts actually show up.";

  const primaryLabel = isMac ? "Grant" : "Check setting";
  const pendingLabel = isMac ? "Asking…" : "Checking…";

  return (
    <Layout
      step={step}
      title={title}
      subtitle={subtitle}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="ghost" onClick={onNext}>
            Skip for now
          </Button>
          {state === "granted" ? (
            <Button onClick={onNext}>Continue</Button>
          ) : state === "denied" ? (
            <Button
              variant="secondary"
              onClick={() => bridge.openNotificationSettings()}
            >
              <ExternalLink className="h-4 w-4" />
              Open Settings
            </Button>
          ) : (
            <Button onClick={handleGrant} disabled={state === "pending"}>
              {state === "pending" ? pendingLabel : primaryLabel}
            </Button>
          )}
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-md border border-ink-border p-4">
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-gray-100 text-ink">
          <Bell className="h-4 w-4" />
        </div>
        <div className="flex-1 space-y-1">
          <h3 className="text-sm font-medium text-ink">Notifications</h3>
          {state === "granted" && (
            <p className="flex items-center gap-1 text-sm font-medium text-green-700">
              <CheckCircle2 className="h-4 w-4" />
              All set.
            </p>
          )}
          {state === "denied" && (
            <p className="flex items-center gap-1 text-sm font-medium text-red-700">
              <XCircle className="h-4 w-4" />
              Notifications are blocked. Open Settings to turn them on, then
              try again.
            </p>
          )}
          {state !== "granted" && state !== "denied" && (
            <p className="text-sm leading-relaxed text-ink-muted">
              {isMac
                ? "macOS will ask once. Click Allow so you don't miss the meeting prompts."
                : "Make sure Sayzo is enabled under Settings → System → Notifications."}
            </p>
          )}
        </div>
      </div>
    </Layout>
  );
}
