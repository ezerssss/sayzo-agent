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

// "stale_tcc" splits off from "denied" so we can show targeted recovery
// copy. macOS silent-denies requestAccess (returns False in milliseconds
// without ever showing the dialog) when EITHER the bundle's Info.plist
// is missing NSMicrophoneUsageDescription OR a previous install left an
// orphan TCC entry whose code-requirement no longer matches the current
// signing identity. In the orphan case the entry is FILTERED OUT of
// System Settings → Privacy & Security → Microphone — the user opens
// the pane and Sayzo isn't there at all, so any "remove from the list"
// instruction is a dead end. Bundle-level recovery via `tccutil reset`
// + relaunch is the only path that works without Terminal.
type State =
  | "idle"
  | "pending"
  | "granted"
  | "denied"
  | "stale_tcc"
  | "resetting";

export function Microphone({ step, onNext, onCancel }: Props) {
  const [state, setState] = useState<State>("idle");

  async function handleAllow() {
    setState("pending");
    try {
      const { granted, stale_tcc_likely } = await bridge.promptMicPermission();
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
      // The bridge call hard-exits the process after `tccutil reset` and
      // `open -n /Applications/Sayzo.app` succeed — this promise never
      // resolves on the success path. We still await it so a failure on
      // the Python side (e.g. tccutil missing in PATH on a stripped
      // managed Mac) flows back as a rejection and we can recover the
      // UI instead of leaving "resetting" stuck on screen.
      await bridge.resetMicPermissionAndRestart();
    } catch {
      // Bridge call failed before relaunch could fire. Drop the user
      // back to the recovery screen so they can try again.
      setState("stale_tcc");
    }
  }

  return (
    <Layout
      step={step}
      title="Microphone access"
      subtitle="Sayzo uses your mic only when you start a conversation — with your keyboard shortcut, or by accepting a prompt on screen."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          {state === "granted" ? (
            <Button onClick={onNext}>Continue</Button>
          ) : state === "denied" ? (
            <Button variant="secondary" onClick={() => bridge.openMicSettings()}>
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
          All set! Your conversations are ready to become personalized
          speaking drills.
        </div>
      )}
      {state === "denied" && (
        <div className="flex items-start gap-2 text-sm text-red-700">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            Looks like macOS blocked the mic. Open System Settings, turn it on
            for Sayzo, then come back.
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
                and we'll clear it for you. The mic dialog should appear
                right after Sayzo relaunches.
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

// Last-resort escalation surfaced in the stale_tcc recovery state.
// `tccutil reset` + relaunch handles every common case (bundle Info.plist
// keys present, orphan TCC entry from a previous install). The cases it
// CAN'T handle (NSMicrophoneUsageDescription actually missing from the
// shipped bundle, managed Mac that strips tccutil from PATH, EDR software
// blocking the relaunch) all need the user to send us their agent.log —
// which they shouldn't have to dig through `~/Library/Application Support`
// to find. Copy + Open Folder buttons make that a one-click affair.
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
    // Reset the button label after a beat so a second click reads as
    // a fresh action, not a stale "copied" affirmation.
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
