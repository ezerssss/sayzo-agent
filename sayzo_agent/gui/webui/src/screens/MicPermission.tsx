import { useState } from "react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  onGranted: () => void;
  onCancel: () => void;
}

export function MicPermission({ onGranted, onCancel }: Props) {
  const [checking, setChecking] = useState(false);
  const [hint, setHint] = useState<string | null>(null);

  async function recheck() {
    setChecking(true);
    setHint(null);
    try {
      const status = await bridge.recheckMacPermission();
      if (status.has_mic_permission) {
        onGranted();
      } else if (status.has_mic_permission === false) {
        setHint(
          "Still blocked. Make sure Sayzo Agent is enabled under both " +
            "Microphone and Audio Capture in Privacy & Security.",
        );
      } else {
        // Unknown — let the user proceed; the runtime will surface a real
        // PermissionError if it really isn't granted.
        onGranted();
      }
    } finally {
      setChecking(false);
    }
  }

  return (
    <Layout
      step="03"
      title="Grant microphone access"
      subtitle="macOS gates microphone and Audio Capture access by app. Open System Settings, enable Sayzo Agent in both lists, then come back here."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={recheck} disabled={checking}>
            {checking ? "Checking…" : "I've granted access"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Button
          variant="secondary"
          onClick={() => bridge.openMacPrivacySettings()}
        >
          Open System Settings
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
