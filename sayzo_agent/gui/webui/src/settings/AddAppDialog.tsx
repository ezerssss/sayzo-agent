import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  settingsBridge,
  DetectorKind,
  DetectorSpecInput,
  DetectorSummary,
  MicHolderSnapshot,
} from "../lib/settings-bridge";
import { Button } from "../components/ui/Button";
import { cn } from "../lib/cn";

const POLL_INTERVAL_MS = 2000;

interface Props {
  initialTab: DetectorKind;
  existing: DetectorSummary[];
  onClose: () => void;
  onAdded: () => void | Promise<void>;
}

export function AddAppDialog({ initialTab, existing, onClose, onAdded }: Props) {
  const [tab, setTab] = useState<DetectorKind>(initialTab);

  // Closing on Escape matches the modal's grab-the-focus behaviour. The
  // backdrop click also closes (handled below).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full max-h-[640px] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl">
        {/* Header */}
        <div className="border-b border-ink-border px-8 pt-7 pb-5">
          <h2 className="text-xl font-semibold tracking-tight text-ink">
            Add a meeting app
          </h2>
          <p className="mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
            Tell Sayzo which apps or sites count as meetings, so it can offer
            to capture them.
          </p>
          <div className="mt-5 inline-flex gap-2">
            <DialogTab
              label="Desktop app"
              selected={tab === "desktop"}
              onClick={() => setTab("desktop")}
            />
            <DialogTab
              label="Web meeting"
              selected={tab === "web"}
              onClick={() => setTab("web")}
            />
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-8 py-6">
          {tab === "desktop" ? (
            <DesktopTab existing={existing} onAdded={onAdded} />
          ) : (
            <WebTab existing={existing} onAdded={onAdded} />
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-ink-border px-8 py-4">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}

interface DialogTabProps {
  label: string;
  selected: boolean;
  onClick: () => void;
}

function DialogTab({ label, selected, onClick }: DialogTabProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md px-4 py-2 text-sm font-medium transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
        selected
          ? "bg-accent text-white focus-visible:ring-accent-ring"
          : "bg-white text-ink border border-ink-border hover:bg-gray-50 focus-visible:ring-ink-border",
      )}
    >
      {label}
    </button>
  );
}

// ---- Desktop tab ----------------------------------------------------------

interface DesktopProps {
  existing: DetectorSummary[];
  onAdded: () => void | Promise<void>;
}

interface DesktopCandidate {
  rawKey: string;
  display: string;
  isBundleId: boolean;
}

function DesktopTab({ existing, onAdded }: DesktopProps) {
  const [candidates, setCandidates] = useState<DesktopCandidate[] | null>(null);
  const [platform, setPlatform] = useState<string>("");
  const [manualOpen, setManualOpen] = useState(false);
  const [manualName, setManualName] = useState("");
  const [manualProcess, setManualProcess] = useState("");
  const [manualBundle, setManualBundle] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Resolve platform once so we know whether to show the bundle-id field
  // (macOS) or process-name field (Windows) on the manual-entry expander.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const info = await settingsBridge.getAboutInfo();
        if (!cancelled) setPlatform(info.platform);
      } catch {
        if (!cancelled) setPlatform("");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Live mic-holder picker — polled every 2 s while the dialog is open. Same
  // cadence as the legacy tkinter dialog. The bridge degrades to an empty
  // snapshot when the agent isn't reachable, so a stopped service shows the
  // empty state instead of a hard error.
  const refresh = useCallback(async () => {
    try {
      const [mic, fg] = await Promise.all([
        settingsBridge.snapshotMicState(),
        settingsBridge.snapshotForeground(),
      ]);

      const taken = new Set<string>();
      for (const spec of existing) {
        for (const p of spec.process_names) taken.add(p.toLowerCase());
        for (const b of spec.bundle_ids) taken.add(b.toLowerCase());
      }

      const seen = new Set<string>();
      const next: DesktopCandidate[] = [];

      for (const h of mic.holders) {
        const key = (h.process_name ?? "").toLowerCase();
        if (!key || seen.has(key) || taken.has(key)) continue;
        const isBrowser = await settingsBridge.isBrowserProcess(h.process_name);
        if (isBrowser) continue;
        seen.add(key);
        next.push({
          rawKey: h.process_name,
          display: prettyProcess(h.process_name),
          isBundleId: false,
        });
      }

      // macOS fallback: foreground bundle id while the mic is active. The
      // platform has no per-process mic attribution, so the watcher leans
      // on "frontmost app + mic.active" the same way.
      if (platform === "darwin" && mic.active && fg.bundle_id && !fg.is_browser) {
        const key = fg.bundle_id.toLowerCase();
        if (!seen.has(key) && !taken.has(key)) {
          next.push({
            rawKey: fg.bundle_id,
            display: prettyBundleId(fg.bundle_id),
            isBundleId: true,
          });
          seen.add(key);
        }
      }

      setCandidates(next);
    } catch {
      setCandidates([]);
    }
  }, [existing, platform]);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const handlePick = useCallback(
    async (c: DesktopCandidate) => {
      const appKey = await settingsBridge.makeAppKey(c.rawKey);
      const spec: DetectorSpecInput = {
        app_key: appKey,
        display_name: c.display || c.rawKey,
        process_names: c.isBundleId ? [] : [c.rawKey],
        bundle_ids: c.isBundleId ? [c.rawKey] : [],
      };
      const result = await settingsBridge.addDetector(spec);
      if (result.added) {
        await onAdded();
      } else {
        setError(result.error ?? "Couldn't add app.");
      }
    },
    [onAdded],
  );

  const handleManualSubmit = useCallback(async () => {
    setError(null);
    const name = manualName.trim();
    const proc = manualProcess.trim();
    const bundle = manualBundle.trim();
    if (!name) {
      setError("Please enter a display name.");
      setManualOpen(true);
      return;
    }
    if (platform === "darwin" && !bundle) {
      setError("Please enter a bundle identifier.");
      setManualOpen(true);
      return;
    }
    if (platform !== "darwin" && !proc) {
      setError("Please enter a process name.");
      setManualOpen(true);
      return;
    }
    const seedKey = bundle || proc;
    const seedKeyLc = seedKey.toLowerCase();
    const taken = new Set<string>();
    for (const spec of existing) {
      for (const p of spec.process_names) taken.add(p.toLowerCase());
      for (const b of spec.bundle_ids) taken.add(b.toLowerCase());
    }
    if (taken.has(seedKeyLc)) {
      setError(`“${seedKey}” is already on your list.`);
      return;
    }
    const appKey = await settingsBridge.makeAppKey(seedKey);
    const spec: DetectorSpecInput = {
      app_key: appKey,
      display_name: name,
      process_names: proc ? [proc] : [],
      bundle_ids: bundle ? [bundle] : [],
    };
    const result = await settingsBridge.addDetector(spec);
    if (result.added) {
      await onAdded();
    } else {
      setError(result.error ?? "Couldn't add app.");
    }
  }, [existing, manualBundle, manualName, manualProcess, onAdded, platform]);

  return (
    <div>
      <h3 className="text-base font-semibold text-ink">
        Pick an app that's using your microphone
      </h3>
      <p className="mt-2 max-w-md text-xs leading-relaxed text-ink-muted">
        Open (or join) your meeting, then click the app below to add it.
        Sayzo reads the apps currently recording from your microphone — nothing
        is sent anywhere.
      </p>

      <div className="mt-5">
        {candidates == null ? (
          <p className="text-sm text-ink-muted">Looking for active apps…</p>
        ) : candidates.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No apps are using your microphone right now. Start a call in the
            app you want to add — it will appear here.
          </p>
        ) : (
          <ul className="space-y-2">
            {candidates.map((c) => (
              <li
                key={c.rawKey}
                className="flex items-center justify-between gap-3 rounded-md border border-ink-border bg-white px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-ink">
                    {c.display}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-ink-muted">
                    {c.rawKey}
                  </div>
                </div>
                <Button variant="secondary" onClick={() => void handlePick(c)}>
                  + Add
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3">
        <Button variant="secondary" onClick={() => void refresh()}>
          Refresh now
        </Button>
      </div>

      <div className="my-6 h-px bg-ink-border" />

      <button
        type="button"
        onClick={() => setManualOpen((v) => !v)}
        className="text-left text-sm text-ink-muted hover:text-ink focus:outline-none"
      >
        {manualOpen ? "▾" : "▸"} Know the app? Add it by name instead
      </button>

      {manualOpen && (
        <div className="mt-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-ink">
              Name it
            </label>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              How this appears in your Meeting Apps list (e.g. “Zoom”, “Team
              standup”).
            </p>
            <input
              type="text"
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
              className="mt-2 w-full rounded-md border border-ink-border bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
            />
          </div>

          {platform === "darwin" ? (
            <div>
              <label className="block text-sm font-medium text-ink">
                Bundle identifier (e.g. com.hnc.Discord)
              </label>
              <input
                type="text"
                value={manualBundle}
                onChange={(e) => setManualBundle(e.target.value)}
                className="mt-2 w-full rounded-md border border-ink-border bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
              />
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                Find it in macOS: open the app, then Apple menu → System
                Information → Applications. The bundle id is in the details
                panel.
              </p>
            </div>
          ) : (
            <div>
              <label className="block text-sm font-medium text-ink">
                Process name (e.g. loom.exe)
              </label>
              <input
                type="text"
                value={manualProcess}
                onChange={(e) => setManualProcess(e.target.value)}
                className="mt-2 w-full rounded-md border border-ink-border bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
              />
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                Find it in Task Manager → Details tab. The process name ends
                in .exe — use the exact filename.
              </p>
            </div>
          )}

          <div>
            <Button variant="primary" onClick={() => void handleManualSubmit()}>
              Add app
            </Button>
          </div>
        </div>
      )}

      {error != null && (
        <p className="mt-4 text-xs text-red-600">{error}</p>
      )}
    </div>
  );
}

// ---- Web tab --------------------------------------------------------------

interface WebProps {
  existing: DetectorSummary[];
  onAdded: () => void | Promise<void>;
}

function WebTab({ existing, onAdded }: WebProps) {
  const [url, setUrl] = useState("");
  const [strict, setStrict] = useState(false);
  const [name, setName] = useState("");
  const [host, setHost] = useState<string | null>(null);
  const [path, setPath] = useState<string>("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Track whether the user has hand-edited the name so a later URL change
  // doesn't clobber their custom label.
  const userEditedName = useRef(false);

  // Re-parse on every URL change. Bridge round-trip is cheap; we don't
  // bother debouncing. The parse result feeds three derived UI bits:
  // preview text, prefilled display name, submit guard.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const trimmed = url.trim();
      if (!trimmed) {
        if (!cancelled) {
          setHost(null);
          setPath("");
          setParseError(null);
        }
        return;
      }
      try {
        const result = await settingsBridge.parseMeetingUrl(trimmed);
        if (cancelled) return;
        if (result.error != null || result.host == null) {
          setHost(null);
          setPath("");
          setParseError(result.error ?? "not_a_url");
        } else {
          setHost(result.host);
          setPath(result.path ?? "");
          setParseError(null);
          if (!userEditedName.current && !name.trim() && result.display_name) {
            setName(result.display_name);
          }
        }
      } catch {
        if (!cancelled) {
          setHost(null);
          setPath("");
          setParseError("not_a_url");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [url, name]);

  const previewText = useMemo(() => {
    if (!url.trim()) return "Paste a URL above to see what it'll match.";
    if (host == null) return "⚠️  That doesn't look like a meeting URL.";
    if (strict && !path) {
      return "⚠️  Strict match needs a path — paste a full meeting URL, or uncheck “Only match this exact meeting”.";
    }
    if (strict) return `${host}${path} — this exact meeting only`;
    return `${host}/… — any meeting on this site`;
  }, [host, path, strict, url]);

  const handleSubmit = useCallback(async () => {
    setSubmitError(null);
    if (host == null) {
      setSubmitError(
        "That doesn't look like a URL — it should have a site like chatgpt.com or meet.google.com/abc-defg-hij.",
      );
      return;
    }
    if (strict && !path) {
      setSubmitError(
        "“Only match this exact meeting” needs a URL with a path (e.g. /j/1234567890). Paste the full meeting URL, or uncheck that option to match the whole site.",
      );
      return;
    }
    const built = await settingsBridge.buildUrlPattern(host, path, strict);
    if (built.error != null || built.pattern == null) {
      setSubmitError(built.error ?? "Couldn't build URL pattern.");
      return;
    }
    const pattern = built.pattern;
    const seed = host + (strict ? path : "");
    const appKey = await settingsBridge.makeAppKey(seed);
    const finalName = name.trim() || host;
    const spec: DetectorSpecInput = {
      app_key: appKey,
      display_name: finalName,
      is_browser: true,
      url_patterns: [pattern],
    };
    // Defensive: warn the user if the same URL pattern is already on their list.
    const dupe = existing.find((d) => d.url_patterns.includes(pattern));
    if (dupe != null) {
      setSubmitError(`That URL is already covered by “${dupe.display_name}”.`);
      return;
    }
    const result = await settingsBridge.addDetector(spec);
    if (result.added) {
      await onAdded();
    } else {
      setSubmitError(result.error ?? "Couldn't add meeting.");
    }
  }, [host, path, strict, name, existing, onAdded]);

  return (
    <div>
      <h3 className="text-base font-semibold text-ink">
        Paste a meeting URL
      </h3>
      <p className="mt-2 max-w-md text-xs leading-relaxed text-ink-muted">
        Copy the URL from the browser tab of a meeting you run regularly.
        Sayzo will ask to start coaching whenever you open that site.
      </p>

      <div className="mt-5">
        <label className="block text-sm font-medium text-ink">URL</label>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://meet.google.com/abc-defg-hij"
          className="mt-2 w-full rounded-md border border-ink-border bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
        />
      </div>

      <div className="mt-3 rounded-md border border-ink-border bg-gray-50 px-3 py-2">
        <div className="text-xs uppercase tracking-wide text-ink-muted">
          Will match:
        </div>
        <div className="mt-1 break-all text-sm font-medium text-ink">
          {previewText}
        </div>
      </div>

      <label className="mt-4 flex items-center gap-2 text-sm text-ink">
        <input
          type="checkbox"
          checked={strict}
          onChange={(e) => setStrict(e.target.checked)}
          className="h-4 w-4 rounded border-ink-border text-accent focus:ring-accent-ring"
        />
        Only match this exact meeting (not every meeting on the site)
      </label>

      <div className="mt-5">
        <label className="block text-sm font-medium text-ink">Name it</label>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          How this appears in your Meeting Apps list. Auto-filled from the
          URL — change it to whatever's easiest to recognize (e.g. “Work
          standup”, “Client calls”).
        </p>
        <input
          type="text"
          value={name}
          onChange={(e) => {
            userEditedName.current = true;
            setName(e.target.value);
          }}
          className="mt-2 w-full rounded-md border border-ink-border bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
        />
      </div>

      <div className="mt-5">
        <Button
          variant="primary"
          onClick={() => void handleSubmit()}
          disabled={host == null || (strict && !path)}
        >
          Add meeting
        </Button>
      </div>

      {(parseError != null && url.trim() !== "" && submitError == null) && (
        <p className="mt-3 text-xs text-ink-muted">
          Keep typing — we'll preview the match once the URL is recognised.
        </p>
      )}
      {submitError != null && (
        <p className="mt-3 text-xs text-red-600">{submitError}</p>
      )}
    </div>
  );
}

// ---- Display name heuristics ---------------------------------------------

// Lightweight equivalents to seen_apps._display_name_for_process /
// _display_name_for_bundle. Kept JS-side so the live picker doesn't have
// to round-trip per-row to format the label.
function prettyProcess(proc: string): string {
  const stem = proc.includes(".") ? proc.split(".").slice(0, -1).join(".") : proc;
  const cleaned = stem.replace(/[-_]/g, " ").trim();
  if (!cleaned) return proc;
  if (cleaned.includes(" ")) {
    return cleaned.replace(/\b\w/g, (m) => m.toUpperCase());
  }
  return cleaned[0].toUpperCase() + cleaned.slice(1);
}

function prettyBundleId(bundleId: string): string {
  const parts = bundleId.split(".");
  const tail = parts.length > 0 ? parts[parts.length - 1] : bundleId;
  const cleaned = tail.replace(/[-_]/g, " ").trim();
  if (!cleaned) return bundleId;
  if (cleaned.includes(" ")) {
    return cleaned.replace(/\b\w/g, (m) => m.toUpperCase());
  }
  return cleaned[0].toUpperCase() + cleaned.slice(1);
}

// MicHolderSnapshot import is referenced only via the bridge type wiring.
// This explicit re-export keeps the type emitted in the .d.ts surface.
export type { MicHolderSnapshot };
