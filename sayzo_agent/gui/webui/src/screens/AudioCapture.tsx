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

  async function handleGrant() {
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
      title="And the other side of your meetings"
      subtitle="So Sayzo can hear the other person in your meetings, not just you."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="ghost" onClick={onNext}>
            Skip for now
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
            <Button onClick={handleGrant} disabled={state === "pending"}>
              {state === "pending" ? "Requesting…" : "Grant"}
            </Button>
          )}
        </>
      }
    >
      {state === "granted" && (
        <div className="flex items-center gap-2 text-sm font-medium text-green-700">
          <CheckCircle2 className="h-4 w-4" />
          System audio capture granted
        </div>
      )}
      {state === "denied" && (
        <div className="flex items-start gap-2 text-sm text-red-700">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Sayzo can't record system audio. Turn it on in System Settings,
            then come back.
          </span>
        </div>
      )}
    </Layout>
  );
}
