import { useEffect } from "react";
import { CheckCircle2 } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

export function Done() {
  // Auto-dismiss after a short pause so the user sees the success state.
  useEffect(() => {
    const t = window.setTimeout(() => {
      void bridge.finish();
    }, 1500);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <Layout
      title="You're set"
      subtitle="Sayzo is listening in the background. Check your menu bar / system tray."
      footer={
        <Button onClick={() => bridge.finish()}>Close</Button>
      }
    >
      <div className="flex items-center gap-3 text-accent">
        <CheckCircle2 className="h-5 w-5" />
        <span className="text-sm font-medium">Setup complete</span>
      </div>
    </Layout>
  );
}
