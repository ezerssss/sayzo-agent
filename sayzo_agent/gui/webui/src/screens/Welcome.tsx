import { useState } from "react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  onSignedIn: () => void;
  onCancel: () => void;
}

export function Welcome({ onSignedIn, onCancel }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSignIn() {
    setError(null);
    setBusy(true);
    try {
      await bridge.startLogin();
    } catch (e) {
      setError(String(e));
      setBusy(false);
      return;
    }
    // Success/failure is reported via the sayzoEvents stream — App listens
    // for `login_done` and advances to the next screen.
    void onSignedIn;
  }

  return (
    <Layout
      step="01"
      title="Welcome to Sayzo"
      subtitle="A two-minute setup so the agent can listen, transcribe locally, and back up your conversations to your account."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={handleSignIn} disabled={busy}>
            {busy ? "Opening browser…" : "Sign in"}
          </Button>
        </>
      }
    >
      <p className="text-sm leading-relaxed text-ink-muted">
        Signing in opens your browser to sayzo.app. Audio always stays on this
        machine — only metadata about each captured session is uploaded.
      </p>
      {error && (
        <Alert className="mt-6">
          <strong>Sign-in failed.</strong> {error}
        </Alert>
      )}
    </Layout>
  );
}
