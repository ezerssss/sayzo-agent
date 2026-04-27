import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CaptureBucket,
  CaptureSummary,
  settingsBridge,
} from "../lib/settings-bridge";
import { Alert } from "../components/ui/Alert";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { SegmentedTab } from "../components/ui/SegmentedTab";
import { cn } from "../lib/cn";

const REFRESH_INTERVAL_MS = 5_000;

const TABS: { bucket: CaptureBucket; label: string }[] = [
  { bucket: "in_progress", label: "In progress" },
  { bucket: "uploaded", label: "Uploaded" },
  { bucket: "failed", label: "Couldn't upload" },
  { bucket: "skipped", label: "Skipped" },
];

const EMPTY_COPY: Record<CaptureBucket, string> = {
  in_progress:
    "Nothing in progress right now. When Sayzo records a meeting, you'll see it here while it's being saved.",
  uploaded:
    "No captures saved to your account yet. Once one finishes uploading, it'll appear here.",
  failed:
    "Nothing failed — Sayzo's keeping up.",
  skipped:
    "Nothing skipped recently. When Sayzo decides not to keep a recording, you'll see why here.",
};

export function CapturesPane() {
  const [captures, setCaptures] = useState<CaptureSummary[] | null>(null);
  const [active, setActive] = useState<CaptureBucket>("in_progress");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (silent = false) => {
    try {
      const list = await settingsBridge.listCaptures();
      setCaptures(list);
      if (!silent) setError(null);
    } catch (e) {
      // Don't blow away the existing list on a transient poll error;
      // just surface the message at the top.
      setError(`Couldn't load your captures: ${String(e)}`);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      void refresh(true);
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const counts = useMemo(() => {
    const out: Record<CaptureBucket, number> = {
      in_progress: 0,
      uploaded: 0,
      failed: 0,
      skipped: 0,
    };
    for (const c of captures ?? []) {
      out[c.bucket] += 1;
    }
    return out;
  }, [captures]);

  const visible = useMemo(() => {
    if (captures == null) return [];
    return captures.filter((c) => c.bucket === active);
  }, [captures, active]);

  const handleRetry = useCallback(
    async (id: string) => {
      try {
        await settingsBridge.retryCaptureUpload(id);
      } catch (e) {
        setError(`Couldn't retry: ${String(e)}`);
        return;
      }
      await refresh();
    },
    [refresh],
  );

  const handleOpen = useCallback(async (id: string) => {
    try {
      await settingsBridge.openCaptureFolder(id);
    } catch (e) {
      setError(`Couldn't open the folder: ${String(e)}`);
    }
  }, []);

  const handleDelete = useCallback(
    async (capture: CaptureSummary) => {
      const ok = window.confirm(
        `Delete this capture? This can't be undone.\n\n${capture.title || "Untitled meeting"}`,
      );
      if (!ok) return;
      // Optimistic remove so the row disappears immediately even if the
      // round-trip takes a beat.
      setCaptures((prev) =>
        prev == null ? prev : prev.filter((c) => c.id !== capture.id),
      );
      try {
        await settingsBridge.deleteCapture(capture.id);
      } catch (e) {
        setError(`Couldn't delete this capture: ${String(e)}`);
      }
      await refresh();
    },
    [refresh],
  );

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Captures
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Sayzo saves your meetings on this computer first, then uploads them to
        your account so you can practice with them. Here's where each one is.
      </p>

      <div className="mt-6 inline-flex flex-wrap gap-2">
        {TABS.map((tab) => (
          <SegmentedTab
            key={tab.bucket}
            label={`${tab.label}${counts[tab.bucket] ? ` (${counts[tab.bucket]})` : ""}`}
            selected={active === tab.bucket}
            onClick={() => setActive(tab.bucket)}
          />
        ))}
      </div>

      {error != null && (
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
      )}

      <div className="mt-4">
        {captures == null ? (
          <p className="text-sm text-ink-muted">Loading…</p>
        ) : visible.length === 0 ? (
          <p className="text-sm text-ink-muted">{EMPTY_COPY[active]}</p>
        ) : (
          <ul className="divide-y divide-ink-border">
            {visible.map((c) => (
              <CaptureRow
                key={c.id}
                capture={c}
                onRetry={() => void handleRetry(c.id)}
                onOpen={() => void handleOpen(c.id)}
                onDelete={() => void handleDelete(c)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

interface CaptureRowProps {
  capture: CaptureSummary;
  onRetry: () => void;
  onOpen: () => void;
  onDelete: () => void;
}

function CaptureRow({ capture, onRetry, onOpen, onDelete }: CaptureRowProps) {
  const title = capture.title?.trim() || "Untitled meeting";

  // Action buttons depend on what the row can actually do.
  const showRetry =
    capture.bucket === "failed" ||
    capture.status === "credit_blocked" ||
    capture.status === "auth_blocked";
  const showOpen = capture.has_audio && capture.bucket !== "skipped";
  // Delete is always available — even processing rows drop out cleanly
  // because they're synthetic; we just hide it for processing to avoid
  // confusion (there's no on-disk state to delete yet).
  const showDelete = !capture.is_processing;

  return (
    <li className="flex items-start justify-between gap-4 py-4">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-ink">{title}</div>
        <div className="mt-0.5 text-xs text-ink-muted">
          <span>{relativeTime(capture.started_at)}</span>
          <span className="mx-1.5">·</span>
          <span>{formatDuration(capture.duration_secs)}</span>
        </div>
        {capture.detail && (
          <div className="mt-1 text-xs text-ink-muted">{capture.detail}</div>
        )}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-2">
        <Badge tone={capture.badge_tone}>{capture.badge_label}</Badge>
        <div className="flex items-center gap-1">
          {showRetry && (
            <Button variant="secondary" onClick={onRetry}>
              Try again now
            </Button>
          )}
          {showOpen && (
            <Button variant="ghost" onClick={onOpen}>
              Open folder
            </Button>
          )}
          {showDelete && (
            <Button
              variant="ghost"
              onClick={onDelete}
              className={cn("hover:text-red-600")}
            >
              Delete
            </Button>
          )}
        </div>
      </div>
    </li>
  );
}

// ---- Formatting helpers ---------------------------------------------------

const RTF = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const diffSecs = Math.round((then - Date.now()) / 1000);
  const absSecs = Math.abs(diffSecs);
  if (absSecs < 60) return RTF.format(diffSecs, "second");
  const diffMins = Math.round(diffSecs / 60);
  if (Math.abs(diffMins) < 60) return RTF.format(diffMins, "minute");
  const diffHours = Math.round(diffMins / 60);
  if (Math.abs(diffHours) < 24) return RTF.format(diffHours, "hour");
  const diffDays = Math.round(diffHours / 24);
  if (Math.abs(diffDays) < 7) return RTF.format(diffDays, "day");
  // For older items, switch to an absolute date so the user sees a real
  // anchor instead of "5 weeks ago".
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(new Date(then));
}

function formatDuration(secs: number): string {
  const total = Math.max(0, Math.round(secs));
  if (total < 60) return `${total}s`;
  const mins = Math.round(total / 60);
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hours}h ${String(remMins).padStart(2, "0")}m` : `${hours}h`;
}
