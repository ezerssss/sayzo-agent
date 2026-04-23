import { useEffect, useState } from "react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { ShortcutCapture } from "../components/ShortcutCapture";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: (binding: string) => void;
  onCancel: () => void;
}

export function Shortcut({ step, onNext, onCancel }: Props) {
  const [binding, setBinding] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void bridge.getHotkey().then((h) => {
      if (!cancelled) setBinding(h.binding);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleContinue() {
    if (binding === null) return;
    setSaving(true);
    setError(null);
    const result = await bridge.saveHotkey(binding);
    setSaving(false);
    if (result.error !== null) {
      setError(result.error);
      return;
    }
    onNext(binding);
  }

  return (
    <Layout
      step={step}
      title="Last thing — pick your shortcut"
      subtitle="This is the key you press when you want Sayzo to start or stop a capture. It's the main way you tell Sayzo to record. You can change it anytime from Settings."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleContinue} disabled={saving || binding === null}>
            {saving ? "Saving…" : "Continue"}
          </Button>
        </>
      }
    >
      {binding !== null && (
        <ShortcutCapture
          initialBinding={binding}
          onChange={(b) => setBinding(b)}
        />
      )}
      {error && (
        <Alert className="mt-6">
          <span>{error}</span>
        </Alert>
      )}
    </Layout>
  );
}
