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
      subtitle="A quick two-minute setup. Nothing records until you say so."
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
        Signing in links this machine to your account so your captures
        become coaching drills in the Sayzo web app.
      </p>
      {error && (
        <Alert className="mt-6">
          <strong>Sign-in failed.</strong> {error}
        </Alert>
      )}
    </Layout>
  );
}
