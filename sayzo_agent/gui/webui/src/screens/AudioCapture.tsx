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

// "stale_tcc" splits off from "denied" so we can show targeted recovery
// copy. The generic "blocked, open settings, turn it on" message is
// actively misleading in the stale-TCC case because System Settings shows
// the toggle ON — the user has nothing left to flip. Same root cause as
// the Microphone screen: a TCC entry from a pre-v2.6.0 ad-hoc-signed
// audio-tap binary whose CR no longer matches the current Developer-ID-
// signed binary.
type State = "idle" | "pending" | "granted" | "denied" | "stale_tcc";

export function AudioCapture({ step, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");

  async function handleAllow() {
    setState("pending");
    try {
      const { granted, stale_tcc_likely } =
        await bridge.promptAudioCapturePermission();
      if (granted === true) {
        setState("granted");
      } else if (granted === false && stale_tcc_likely) {
        setState("stale_tcc");
      } else if (granted === false) {
        setState("denied");
      } else {
        setState("idle");
      }
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
          ) : state === "stale_tcc" ? (
            <>
              <Button
                variant="secondary"
                onClick={() => bridge.openAudioCaptureSettings()}
              >
                Open Settings
              </Button>
              <Button onClick={handleAllow}>Try Again</Button>
            </>
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
      {state === "stale_tcc" && (
        <div className="flex items-start gap-2 text-sm text-amber-800">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-2">
            <p className="font-medium">
              macOS still has a system-audio entry from a previous Sayzo
              install — that's why you don't see a dialog.
            </p>
            <p>
              Open System Settings → Privacy &amp; Security → Audio Capture,
              click <span className="font-mono">Sayzo</span> in the list,
              then click the <span className="font-mono">−</span> button at
              the bottom to remove it. Come back here and click Try Again.
            </p>
          </div>
        </div>
      )}
    </Layout>
  );
}
