import { useEffect, useRef, useState } from "react";
import { Button } from "../components/ui/Button";
import { Progress } from "../components/ui/Progress";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";
import { subscribe, SayzoEvent } from "../lib/events";

interface Props {
  onDone: () => void;
  onCancel: () => void;
}

function formatGB(bytes: number): string {
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

export function Download({ onDone, onCancel }: Props) {
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [status, setStatus] = useState<"idle" | "downloading" | "done" | "error">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);
  const startedRef = useRef(false);

  useEffect(() => {
    return subscribe((evt: SayzoEvent) => {
      if (evt.type === "download_progress") {
        setProgress({ done: evt.done, total: evt.total });
        setStatus("downloading");
      } else if (evt.type === "download_done") {
        setStatus("done");
        setProgress((p) => ({ done: p.total || p.done, total: p.total }));
      } else if (evt.type === "download_error") {
        setStatus("error");
        setError(evt.message);
      }
    });
  }, []);

  async function start() {
    if (startedRef.current) return;
    startedRef.current = true;
    setStatus("downloading");
    setError(null);
    try {
      await bridge.startModelDownload();
    } catch (e) {
      setStatus("error");
      setError(String(e));
      startedRef.current = false;
    }
  }

  // Auto-start on mount.
  useEffect(() => {
    void start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pct =
    progress.total > 0 ? (progress.done / progress.total) * 100 : 0;

  return (
    <Layout
      step="02"
      title="Setting things up"
      subtitle="Getting Sayzo ready — about 2 GB, one time only."
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={onDone} disabled={status !== "done"}>
            Continue
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Progress
          value={pct}
          indeterminate={status === "downloading" && progress.total === 0}
        />
        <div className="flex justify-between text-xs tabular-nums text-ink-muted">
          <span>
            {progress.total > 0
              ? `${formatGB(progress.done)} / ${formatGB(progress.total)}`
              : status === "idle"
                ? "Preparing download…"
                : "Connecting…"}
          </span>
          <span>{progress.total > 0 ? `${pct.toFixed(0)}%` : ""}</span>
        </div>
      </div>

      {status === "error" && error && (
        <Alert className="mt-6">
          <div className="space-y-2">
            <div>
              <strong>Download failed.</strong> {error}
            </div>
            <Button
              variant="secondary"
              onClick={() => {
                startedRef.current = false;
                void start();
              }}
            >
              Retry
            </Button>
          </div>
        </Alert>
      )}
    </Layout>
  );
}
