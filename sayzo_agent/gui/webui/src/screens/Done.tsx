import { useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  hotkeyDisplay: string;
}

// Last screen before the setup window closes. Copy has to match the
// armed-only invariant: Sayzo does NOT listen continuously. The mic stays
// closed until the user presses the hotkey OR accepts a meeting-detect
// consent prompt. Anything that sounds like "always listening in the
// background" is a bug — it's the whole point of the rewrite.
export function Done({ hotkeyDisplay }: Props) {
  const [finishing, setFinishing] = useState(false);

  async function handleFinish() {
    setFinishing(true);
    try {
      await bridge.markPermissionsOnboarded();
    } finally {
      void bridge.finish();
    }
  }

  // Mark onboarded + close on Enter, so pressing return after the last
  // screen feels responsive.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Enter" && !finishing) {
        void handleFinish();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finishing]);

  return (
    <Layout
      title="You're all set"
      subtitle={`Press ${hotkeyDisplay} to start a capture, or say yes when Sayzo spots a meeting. That's it — nothing records until you say so.`}
      footer={
        <Button onClick={handleFinish} disabled={finishing}>
          {finishing ? "Closing…" : "Got it"}
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-3 text-accent">
          <CheckCircle2 className="h-5 w-5" />
          <span className="text-sm font-medium">Setup complete</span>
        </div>
        <p className="text-sm leading-relaxed text-ink-muted">
          Sayzo lives in your menu bar — click it any time to start, stop,
          or open Settings.
        </p>
      </div>
    </Layout>
  );
}
