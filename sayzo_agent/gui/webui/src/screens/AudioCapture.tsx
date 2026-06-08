import { useState } from "react";
import { CheckCircle2, ClipboardCheck, FolderOpen, XCircle } from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";

interface Props {
  step: string;
  onNext: () => void;
  onCancel: () => void;
}

// Same shape as the Microphone screen's stale_tcc state. v2.7.4
// established that the most common reason the system audio dialog
// doesn't appear is a missing Hardened-Runtime entitlement at codesign
// time. Other possible causes — orphan TCC entry, missing usage
// description — are rarer post-v2.7.4. Recovery actions are the same
// either way: Reset & Restart (clears any orphan entry as a safety
// belt) plus Copy diagnostic / Open log folder for support.
type State =
  | "idle"
  | "pending"
  | "granted"
  | "denied"
  | "stale_tcc"
  | "resetting";

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

  async function handleResetAndRestart() {
    setState("resetting");
    try {
      // Bridge hard-exits after tccutil + relaunch fire; this promise
      // never resolves on the happy path. A rejection means Python
      // failed before relaunch, so we recover back to the recovery UI
      // instead of leaving "resetting" stuck on screen.
      await bridge.resetAudioCapturePermissionAndRestart();
    } catch {
      setState("stale_tcc");
    }
  }

  return (
    <Layout
      step={step}
      title="System audio access"
      subtitle="Sayzo captures audio from your meetings (like Zoom, Meet, or Teams) so it can transcribe the full conversation, not just your side."
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
            <Button onClick={handleResetAndRestart}>
              Reset &amp; Restart Sayzo
            </Button>
          ) : state === "resetting" ? (
            <Button disabled>Restarting…</Button>
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
          All set! Sayzo will now coach you on the whole meeting, not just
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
        <>
          <div className="flex items-start gap-2 text-sm text-amber-800">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="space-y-2">
              <p className="font-medium">
                macOS didn't show the permission dialog.
              </p>
              <p>
                This usually means a leftover entry from a previous Sayzo
                install is silently blocking us — and macOS hides it from
                System Settings, so there's nothing visible for you to toggle.
                Click <span className="font-medium">Reset &amp; Restart Sayzo</span>{" "}
                and we'll clear it for you. The system audio dialog should
                appear right after Sayzo relaunches.
              </p>
            </div>
          </div>
          <StuckHelp />
        </>
      )}
      {state === "resetting" && (
        <div className="flex items-center gap-2 text-sm font-medium text-ink">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
          Clearing the leftover permission and restarting Sayzo…
        </div>
      )}
    </Layout>
  );
}

// Mirrors the StuckHelp on Microphone.tsx (kept duplicated rather than
// shared from a common module — both screens are tiny, the helper is a
// dozen lines, and bridging through a shared component would force an
// extra import path for ~20 lines of saved code).
function StuckHelp() {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );

  async function handleCopy() {
    try {
      const { copied } = await bridge.copyTccDiagnosticToClipboard();
      setCopyState(copied ? "copied" : "failed");
    } catch {
      setCopyState("failed");
    }
    window.setTimeout(() => setCopyState("idle"), 2500);
  }

  return (
    <div className="mt-4 rounded-md border border-ink-border/60 bg-ink-muted/5 p-3">
      <p className="text-xs font-medium text-ink">
        Still stuck after Reset &amp; Restart?
      </p>
      <p className="mt-1 text-xs leading-relaxed text-ink-muted">
        Copy a diagnostic snapshot (bundle info, code-signing details, recent
        log lines) and send it to{" "}
        <span className="font-mono">support@sayzo.app</span>. The log folder
        button opens <span className="font-mono">agent.log</span> directly so
        you can attach it.
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1.5 rounded-md border border-ink-border bg-white px-2.5 py-1 text-xs font-medium text-ink hover:bg-ink-muted/10"
        >
          <ClipboardCheck className="h-3.5 w-3.5" />
          {copyState === "copied"
            ? "Copied!"
            : copyState === "failed"
              ? "Copy failed — try again"
              : "Copy diagnostic info"}
        </button>
        <button
          type="button"
          onClick={() => {
            void bridge.openLogFolder();
          }}
          className="inline-flex items-center gap-1.5 rounded-md border border-ink-border bg-white px-2.5 py-1 text-xs font-medium text-ink hover:bg-ink-muted/10"
        >
          <FolderOpen className="h-3.5 w-3.5" />
          Open log folder
        </button>
      </div>
    </div>
  );
}
