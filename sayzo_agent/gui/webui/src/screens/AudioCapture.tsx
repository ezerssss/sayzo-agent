import { useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: () => void;
  onCancel: () => void;
}

type State = "idle" | "pending" | "granted" | "denied";

export function AudioCapture({ step, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");

  async function handleAllow() {
    setState("pending");
    try {
      const { granted } = await bridge.promptAudioCapturePermission();
      setState(granted === true ? "granted" : granted === false ? "denied" : "idle");
    } catch {
      setState("idle");
    }
  }

  return (
    <Layout
      step={step}
      title="System audio access"
      subtitle="Sayzo captures audio from your meetings — like Zoom, Meet, or Teams — so it can transcribe the full conversation, not just your side."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          {state === "granted" ? (
            <Button onClick={onNext}>Continue</Button>
          ) : state === "denied" ? (
            <Button
              variant="secondary"
              onClick={() => bridge.openAudioCaptureSettings()}
            >
              Open Settings
            </Button>
          ) : (
            <Button onClick={handleAllow} disabled={state === "pending"}>
              {state === "pending" ? "Requesting…" : "Allow"}
            </Button>
          )}
        </>
      }
    >
      {state === "granted" && (
        <div className="flex items-center gap-2 text-sm font-medium text-green-700">
          <CheckCircle2 className="h-4 w-4" />
          All set! Your drills will now use the whole meeting — not just
          your side.
        </div>
      )}
      {state === "denied" && (
        <div className="flex items-start gap-2 text-sm text-red-700">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Looks like macOS blocked system audio. Open System Settings, turn
            it on for Sayzo, then come back.
          </span>
        </div>
      )}
    </Layout>
  );
}
