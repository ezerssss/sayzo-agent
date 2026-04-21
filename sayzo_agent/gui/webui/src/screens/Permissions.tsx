import { useState } from "react";
import {
  Bell,
  CheckCircle2,
  ExternalLink,
  Mic,
  Volume2,
  XCircle,
} from "lucide-react";
import { Button } from "../components/ui/Button";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";
import { cn } from "../lib/cn";

type RowId = "mic" | "audio-capture" | "notifications";
type RowState = "idle" | "pending" | "granted" | "denied";

interface Row {
  id: RowId;
  icon: typeof Mic;
  title: string;
  body: string;
  prompt: () => Promise<{ granted: boolean | null }>;
  openSettings: () => Promise<null>;
}

const ROWS: Row[] = [
  {
    id: "mic",
    icon: Mic,
    title: "Microphone",
    body: "Sayzo listens to your side of the conversation so it can transcribe what you say and give you coaching feedback.",
    prompt: () => bridge.promptMicPermission(),
    openSettings: () => bridge.openMicSettings(),
  },
  {
    id: "audio-capture",
    icon: Volume2,
    title: "Audio Capture",
    body: "Sayzo also records audio from other apps (Zoom, Meet, FaceTime, etc.) so it can hear the person you're talking to.",
    prompt: () => bridge.promptAudioCapturePermission(),
    openSettings: () => bridge.openAudioCaptureSettings(),
  },
  {
    id: "notifications",
    icon: Bell,
    title: "Notifications",
    body: "A short toast after each saved conversation so you know it landed. Optional — the agent works either way.",
    prompt: () => bridge.promptNotificationPermission(),
    openSettings: () => bridge.openNotificationSettings(),
  },
];

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

export function Permissions({ onDone, onCancel }: Props) {
  const [rowStates, setRowStates] = useState<Record<RowId, RowState>>({
    mic: "idle",
    "audio-capture": "idle",
    notifications: "idle",
  });
  const [finishing, setFinishing] = useState(false);

  async function handleGrant(row: Row) {
    setRowStates((s) => ({ ...s, [row.id]: "pending" }));
    try {
      const { granted } = await row.prompt();
      setRowStates((s) => ({
        ...s,
        [row.id]: granted === true ? "granted" : granted === false ? "denied" : "idle",
      }));
    } catch {
      // Bridge error — flatten to idle so the user can retry. The Python
      // side also logs warnings.
      setRowStates((s) => ({ ...s, [row.id]: "idle" }));
    }
  }

  async function handleContinue() {
    setFinishing(true);
    try {
      await bridge.markPermissionsOnboarded();
    } finally {
      // Always advance — the marker is a nice-to-have, not load-bearing.
      onDone();
    }
  }

  const anyDenied = Object.values(rowStates).some((s) => s === "denied");

  return (
    <Layout
      step="03"
      title="Give Sayzo access"
      subtitle="Sayzo runs entirely on your machine. Here's what it needs permission to do, and why. Click Grant on each — macOS will show its own prompt when you do."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={finishing}>
            Cancel
          </Button>
          <Button onClick={handleContinue} disabled={finishing}>
            {finishing ? "Saving…" : "Continue"}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {ROWS.map((row) => (
          <PermissionRow
            key={row.id}
            row={row}
            state={rowStates[row.id]}
            onGrant={() => handleGrant(row)}
            onOpenSettings={() => row.openSettings()}
          />
        ))}
      </div>

      <div className="mt-8 rounded-md border border-ink-border bg-gray-50 p-4">
        <p className="text-sm leading-relaxed text-ink-muted">
          You can change any of these anytime in{" "}
          <span className="font-medium text-ink">
            System Settings → Privacy &amp; Security
          </span>
          .
        </p>
        <Button
          variant="secondary"
          className="mt-3"
          onClick={() => bridge.openMicSettings()}
        >
          <ExternalLink className="h-4 w-4" />
          Open System Settings
        </Button>
      </div>

      {anyDenied && (
        <p className="mt-4 text-xs leading-relaxed text-ink-muted">
          You can still continue — Sayzo will skip the features you didn't
          allow until you grant permission later.
        </p>
      )}
    </Layout>
  );
}

interface RowProps {
  row: Row;
  state: RowState;
  onGrant: () => void;
  onOpenSettings: () => void;
}

function PermissionRow({ row, state, onGrant, onOpenSettings }: RowProps) {
  const Icon = row.icon;
  return (
    <div className="flex items-start gap-3 rounded-md border border-ink-border p-4">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-gray-100 text-ink">
        <Icon className="h-4 w-4" />
      </div>
      <div className="flex-1 space-y-1">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium text-ink">{row.title}</h3>
          <StatusPill state={state} />
        </div>
        <p className="text-sm leading-relaxed text-ink-muted">{row.body}</p>
      </div>
      <div className="shrink-0">
        {state === "denied" ? (
          <Button variant="secondary" onClick={onOpenSettings}>
            Open Settings
          </Button>
        ) : state === "granted" ? null : (
          <Button
            variant="primary"
            onClick={onGrant}
            disabled={state === "pending"}
          >
            {state === "pending" ? "Requesting…" : "Grant"}
          </Button>
        )}
      </div>
    </div>
  );
}

function StatusPill({ state }: { state: RowState }) {
  if (state === "granted") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-xs font-medium text-green-700">
        <CheckCircle2 className="h-3 w-3" />
        Granted
      </span>
    );
  }
  if (state === "denied") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">
        <XCircle className="h-3 w-3" />
        Denied
      </span>
    );
  }
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-ink-muted",
      )}
    >
      Not asked
    </span>
  );
}
