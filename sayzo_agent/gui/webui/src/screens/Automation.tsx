import { useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: () => void;
  onCancel: () => void;
}

export function Automation({ step, onNext, onCancel }: Props) {
  const [prompted, setPrompted] = useState<string[] | null>(null);
  const [pending, setPending] = useState(false);

  async function handleGrant() {
    setPending(true);
    try {
      const { prompted } = await bridge.promptAutomationPermission();
      setPrompted(prompted);
    } finally {
      setPending(false);
    }
  }

  return (
    <Layout
      step={step}
      title="Know when you're in a web meeting"
      subtitle="So Sayzo can tell you're in Google Meet or Teams, instead of just browsing. Only the tab's URL — never what's on the page."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="ghost" onClick={onNext}>
            Skip for now
          </Button>
          {prompted !== null ? (
            <Button onClick={onNext}>Continue</Button>
          ) : (
            <Button onClick={handleGrant} disabled={pending}>
              {pending ? "Requesting…" : "Grant (per browser)"}
            </Button>
          )}
        </>
      }
    >
      {prompted !== null && prompted.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm font-medium text-green-700">
            <CheckCircle2 className="h-4 w-4" />
            macOS will ask once per browser.
          </div>
          <p className="text-sm leading-relaxed text-ink-muted">
            Click OK on each prompt ({prompted.join(", ")}), then press
            Continue.
          </p>
        </div>
      )}
      {prompted !== null && prompted.length === 0 && (
        <p className="text-sm leading-relaxed text-ink-muted">
          No supported browsers found. You can skip this step.
        </p>
      )}
    </Layout>
  );
}
