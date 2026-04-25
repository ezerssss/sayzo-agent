import { useCallback, useEffect, useMemo, useState } from "react";
import {
  settingsBridge,
  DetectorKind,
  DetectorSummary,
  DetectorSpecInput,
  SeenAppSummary,
} from "../lib/settings-bridge";
import { Button } from "../components/ui/Button";
import { SegmentedTab } from "../components/ui/SegmentedTab";
import { Switch } from "../components/ui/Switch";
import { cn } from "../lib/cn";
import { AddAppDialog } from "./AddAppDialog";

const UNDO_TIMEOUT_MS = 8000;

// One section's worth of state. The undo banner re-uses the same shape so
// "Reset to defaults" can stash the pre-reset list and restore it wholesale
// if the user clicks Undo within the timeout.
interface UndoSnapshot {
  label: string;
  detectors: DetectorSpecInput[];
}

export function MeetingAppsPane() {
  const [detectors, setDetectors] = useState<DetectorSummary[] | null>(null);
  const [seen, setSeen] = useState<SeenAppSummary[]>([]);
  const [section, setSection] = useState<DetectorKind>("desktop");
  const [dialogTab, setDialogTab] = useState<DetectorKind | null>(null);
  const [undo, setUndo] = useState<UndoSnapshot | null>(null);

  const refresh = useCallback(async () => {
    const [listResult, seenResult] = await Promise.allSettled([
      settingsBridge.listDetectors(),
      settingsBridge.listSeenApps(),
    ]);
    setDetectors(listResult.status === "fulfilled" ? listResult.value : []);
    setSeen(seenResult.status === "fulfilled" ? seenResult.value : []);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Auto-clear the undo banner after the timeout so it never stays around
  // forever if the user just keeps clicking around without dismissing it.
  useEffect(() => {
    if (undo == null) return undefined;
    const id = window.setTimeout(() => setUndo(null), UNDO_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [undo]);

  const handleToggle = useCallback(
    async (appKey: string, enabled: boolean) => {
      // Optimistic — flip immediately so the Switch animates without a
      // round-trip lag, then roll back if persistence fails.
      setDetectors((cur) =>
        cur
          ? cur.map((d) =>
              d.app_key === appKey ? { ...d, disabled: !enabled } : d,
            )
          : cur,
      );
      try {
        const result = await settingsBridge.toggleDetector(appKey, enabled);
        if (!result.saved) {
          setDetectors((cur) =>
            cur
              ? cur.map((d) =>
                  d.app_key === appKey ? { ...d, disabled: enabled } : d,
                )
              : cur,
          );
        }
      } catch {
        setDetectors((cur) =>
          cur
            ? cur.map((d) =>
                d.app_key === appKey ? { ...d, disabled: enabled } : d,
              )
            : cur,
        );
      }
    },
    [],
  );

  const handleRemove = useCallback(
    async (appKey: string) => {
      if (detectors == null) return;
      const removed = detectors.find((d) => d.app_key === appKey);
      if (removed == null) return;
      // Snapshot the whole pre-removal list so Undo restores the exact
      // ordering — the bridge's persist path rewrites the detectors array
      // wholesale anyway, so a per-spec re-add wouldn't preserve order.
      setUndo({
        label: `Removed “${removed.display_name}”.`,
        detectors: detectors.map(detectorSummaryToInput),
      });
      try {
        await settingsBridge.removeDetector(appKey);
      } catch {
        // Persistence failure: refresh from disk to re-sync the UI.
      }
      await refresh();
    },
    [detectors, refresh],
  );

  const handleReset = useCallback(async () => {
    if (detectors == null) return;
    setUndo({
      label: "Restored the built-in app list.",
      detectors: detectors.map(detectorSummaryToInput),
    });
    try {
      await settingsBridge.resetDetectors();
    } catch {
      // Best-effort.
    }
    await refresh();
  }, [detectors, refresh]);

  const handleUndo = useCallback(async () => {
    if (undo == null) return;
    // Replay: clear the override (so we start from a known empty slate)
    // then re-add each spec in its original order. We can't write the
    // list directly, so add_detector() in order is the cleanest path.
    try {
      await settingsBridge.resetDetectors();
      for (const spec of undo.detectors) {
        await settingsBridge.addDetector(spec);
      }
    } catch {
      // Best-effort.
    }
    setUndo(null);
    await refresh();
  }, [undo, refresh]);

  const handleAddSeen = useCallback(
    async (s: SeenAppSummary) => {
      const seedKey = s.process_name ?? s.bundle_id ?? s.key;
      const appKey = await settingsBridge.makeAppKey(seedKey);
      const spec: DetectorSpecInput = {
        app_key: appKey,
        display_name: s.display_name || s.key,
        process_names: s.process_name ? [s.process_name] : [],
        bundle_ids: s.bundle_id ? [s.bundle_id] : [],
      };
      try {
        await settingsBridge.addDetector(spec);
        await settingsBridge.dismissSeenApp(s.key);
      } catch {
        // Best-effort.
      }
      await refresh();
    },
    [refresh],
  );

  const handleDismissSeen = useCallback(
    async (s: SeenAppSummary) => {
      try {
        await settingsBridge.dismissSeenApp(s.key);
      } catch {
        // Best-effort.
      }
      await refresh();
    },
    [refresh],
  );

  const visible = useMemo(() => {
    if (detectors == null) return [];
    return detectors.filter((d) =>
      section === "web" ? d.is_browser : !d.is_browser,
    );
  }, [detectors, section]);

  if (detectors == null) {
    return (
      <div className="text-sm text-ink-muted">Loading meeting apps…</div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Meeting Apps
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Sayzo asks to start coaching when one of these apps is in a meeting.
        Toggle an app off to stop matching it without losing its settings.
      </p>

      {/* Section tabs — Desktop apps / Web meetings, segmented style. */}
      <div className="mt-6 inline-flex gap-2">
        <SegmentedTab
          label="Desktop apps"
          selected={section === "desktop"}
          onClick={() => setSection("desktop")}
        />
        <SegmentedTab
          label="Web meetings"
          selected={section === "web"}
          onClick={() => setSection("web")}
        />
      </div>

      {/* Action bar: + Add <section> on the left, Reset on the right. */}
      <div className="mt-4 flex items-center justify-between">
        <Button variant="primary" onClick={() => setDialogTab(section)}>
          {section === "desktop" ? "+ Add desktop app" : "+ Add web meeting"}
        </Button>
        <Button variant="ghost" onClick={() => void handleReset()}>
          Reset to defaults
        </Button>
      </div>

      {/* Undo banner (slot between actions and list, mirrors tkinter). */}
      {undo != null && (
        <div className="mt-4 flex items-center justify-between rounded-md border border-accent bg-accent/10 px-3 py-2 text-sm text-ink">
          <span>{undo.label}</span>
          <Button variant="secondary" onClick={() => void handleUndo()}>
            Undo
          </Button>
        </div>
      )}

      {/* Detector list. */}
      <div className="mt-4">
        {visible.length === 0 ? (
          <p className="text-sm text-ink-muted">
            {section === "web"
              ? "No web meetings on your list. Click “+ Add web meeting” above to add one from a URL."
              : "No desktop apps on your list. Click “+ Add desktop app” above to add one — or start a meeting and Sayzo will suggest it automatically."}
          </p>
        ) : (
          <ul className="divide-y divide-ink-border">
            {visible.map((d) => (
              <DetectorRow
                key={d.app_key}
                detector={d}
                onToggle={(enabled) => void handleToggle(d.app_key, enabled)}
                onRemove={() => void handleRemove(d.app_key)}
              />
            ))}
          </ul>
        )}
      </div>

      {/* Suggested-to-add — Desktop tab only, since the watcher skips
          browsers when recording seen apps. */}
      {section === "desktop" && seen.length > 0 && (
        <section className="mt-10">
          <div className="text-xs font-medium uppercase tracking-wide text-ink-muted">
            Suggested to add
          </div>
          <p className="mt-1 max-w-md text-xs text-ink-muted">
            Apps Sayzo saw using your microphone that aren't on your list yet.
          </p>
          <ul className="mt-3 space-y-2">
            {seen.slice(0, 5).map((s) => (
              <SuggestedRow
                key={s.key}
                seen={s}
                onAdd={() => void handleAddSeen(s)}
                onDismiss={() => void handleDismissSeen(s)}
              />
            ))}
          </ul>
        </section>
      )}

      {dialogTab != null && (
        <AddAppDialog
          initialTab={dialogTab}
          existing={detectors}
          onClose={() => setDialogTab(null)}
          onAdded={async () => {
            setDialogTab(null);
            await refresh();
          }}
        />
      )}
    </div>
  );
}

interface DetectorRowProps {
  detector: DetectorSummary;
  onToggle: (enabled: boolean) => void;
  onRemove: () => void;
}

function DetectorRow({ detector, onToggle, onRemove }: DetectorRowProps) {
  const muted = detector.disabled;
  return (
    <li className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0 flex-1">
        <div
          className={cn(
            "truncate text-sm font-medium",
            muted ? "text-ink-muted" : "text-ink",
          )}
        >
          {detector.display_name}
        </div>
        <div className="mt-0.5 truncate text-xs text-ink-muted">
          {detector.detail}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <Switch
          checked={!detector.disabled}
          onChange={onToggle}
          ariaLabel={`Match ${detector.display_name}`}
        />
        <Button variant="ghost" onClick={onRemove}>
          Remove
        </Button>
      </div>
    </li>
  );
}

interface SuggestedRowProps {
  seen: SeenAppSummary;
  onAdd: () => void;
  onDismiss: () => void;
}

function SuggestedRow({ seen, onAdd, onDismiss }: SuggestedRowProps) {
  const detail = seen.process_name ?? seen.bundle_id ?? seen.key;
  return (
    <li className="flex items-center justify-between gap-4 rounded-md border border-ink-border bg-white px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-ink">
          {seen.display_name}
        </div>
        <div className="mt-0.5 truncate text-xs text-ink-muted">{detail}</div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="secondary" onClick={onAdd}>
          + Add
        </Button>
        <Button variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>
    </li>
  );
}

// The Undo flow rebuilds the detector list spec-by-spec via add_detector(),
// so we need to round-trip a DetectorSummary back into the input shape
// pydantic validates against. Drops the bridge-derived `kind` / `detail`
// fields (they aren't on `DetectorSpec`).
function detectorSummaryToInput(d: DetectorSummary): DetectorSpecInput {
  return {
    app_key: d.app_key,
    display_name: d.display_name,
    is_browser: d.is_browser,
    process_names: d.process_names,
    bundle_ids: d.bundle_ids,
    url_patterns: d.url_patterns,
    title_patterns: d.title_patterns,
    disabled: d.disabled,
  };
}
