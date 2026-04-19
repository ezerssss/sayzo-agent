import { CheckCircle2 } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

// The Done screen is the last thing the user sees before the setup window
// closes and the agent runs silently in the background. We intentionally do
// NOT auto-dismiss — the user deserves a moment to see the success state and
// click through on their own timing. The "Start listening" button fires
// bridge.finish() which closes the window and hands control back to the
// service, which then boots the tray + capture pipeline.
export function Done() {
  return (
    <Layout
      title="You're all set"
      subtitle="Sayzo will run quietly in the background and pick up conversations as they happen. Check the menu bar / system tray any time to pause or quit."
      footer={
        <Button onClick={() => bridge.finish()}>Got it</Button>
      }
    >
      <div className="flex items-center gap-3 text-accent">
        <CheckCircle2 className="h-5 w-5" />
        <span className="text-sm font-medium">Setup complete</span>
      </div>
    </Layout>
  );
}
