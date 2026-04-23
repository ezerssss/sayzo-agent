import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: () => void;
  onCancel: () => void;
}

export function Accessibility({ step, onNext, onCancel }: Props) {
  const [opened, setOpened] = useState(false);

  async function handleOpen() {
    const { opened } = await bridge.openAccessibilitySettings();
    setOpened(opened);
  }

  return (
    <Layout
      step={step}
      title="Let the shortcut work anywhere"
      subtitle="Without this, your shortcut only works when Sayzo is focused. You can always grant it later from System Settings."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="ghost" onClick={onNext}>
            Skip for now
          </Button>
          {opened ? (
            <Button onClick={onNext}>Continue</Button>
          ) : (
            <Button onClick={handleOpen}>
              <ExternalLink className="h-4 w-4" />
              Open System Settings
            </Button>
          )}
        </>
      }
    >
      {opened && (
        <p className="text-sm leading-relaxed text-ink-muted">
          Find <strong>Sayzo Agent</strong> under Accessibility, turn it on,
          then come back and press Continue.
        </p>
      )}
    </Layout>
  );
}
