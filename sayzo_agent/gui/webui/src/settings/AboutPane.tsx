import { useEffect, useState } from "react";
import { settingsBridge, AboutInfo } from "../lib/settings-bridge";
import { subscribe, SayzoEvent } from "../lib/events";
import { useCopyToClipboard } from "../lib/useCopyToClipboard";
import { Button } from "../components/ui/Button";
import { Switch } from "../components/ui/Switch";
import logoUrl from "../assets/logo.png";

type CheckState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "latest" }
  | { kind: "available"; version: string; url: string }
  | { kind: "downloading"; version: string; percent: number }
  | { kind: "applying"; version: string }
  | { kind: "queued"; version: string }
  | { kind: "error"; message?: string };

const CHECK_LABELS: Record<CheckState["kind"], string> = {
  idle: "Check for updates",
  checking: "Checking…",
  latest: "Check again",
  available: "Check again",
  downloading: "Downloading…",
  applying: "Installing…",
  queued: "Check again",
  error: "Try again",
};

export function AboutPane() {
  const [info, setInfo] = useState<AboutInfo | null>(null);
  const [check, setCheck] = useState<CheckState>({ kind: "idle" });
  const { copied, copy } = useCopyToClipboard();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const a = await settingsBridge.getAboutInfo();
        if (!cancelled) setInfo(a);
      } catch {
        // Surface as missing info rather than blocking the whole pane.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return subscribe((evt: SayzoEvent) => {
      if (evt.type === "update_result") {
        if (evt.has_update && evt.version && evt.url) {
          setCheck({ kind: "available", version: evt.version, url: evt.url });
        } else {
          setCheck({ kind: "latest" });
        }
      } else if (evt.type === "update_error") {
        setCheck({ kind: "error", message: evt.message });
      } else if (evt.type === "update_phase") {
        if (evt.phase === "downloading" && evt.version) {
          setCheck({
            kind: "downloading",
            version: evt.version,
            percent: evt.percent ?? 0,
          });
        } else if (evt.phase === "applying" && evt.version) {
          setCheck({ kind: "applying", version: evt.version });
        } else if (evt.phase === "queued_for_restart" && evt.version) {
          setCheck({ kind: "queued", version: evt.version });
        } else if (evt.phase === "noop_already_latest") {
          setCheck({ kind: "latest" });
        } else if (evt.phase === "error") {
          setCheck({ kind: "error", message: evt.message });
        }
      }
    });
  }, []);

  async function handleCheck() {
    setCheck({ kind: "checking" });
    try {
      await settingsBridge.checkForUpdate();
    } catch {
      setCheck({ kind: "error" });
    }
  }

  async function handleInstall(version: string) {
    // Optimistic transition so the button can't be re-clicked while the
    // worker spins up. The real downloading event arrives within ~100ms
    // and replaces this state.
    setCheck({ kind: "downloading", version, percent: 0 });
    try {
      await settingsBridge.installUpdateNow();
    } catch {
      setCheck({
        kind: "error",
        message: "Couldn't start the install. Try again.",
      });
    }
  }

  async function handleCopyDiagnostics() {
    try {
      const { text } = await settingsBridge.getDiagnostics();
      await copy(text);
    } catch {
      // getDiagnostics failed — leave the button as-is.
    }
  }

  async function handleShareDiagnostics(value: boolean) {
    // Optimistic flip; revert if the bridge reports it didn't save.
    setInfo((cur) => (cur ? { ...cur, share_diagnostics: value } : cur));
    try {
      const result = await settingsBridge.setShareDiagnostics(value);
      if (!result.saved) {
        setInfo((cur) => (cur ? { ...cur, share_diagnostics: !value } : cur));
      }
    } catch {
      setInfo((cur) => (cur ? { ...cur, share_diagnostics: !value } : cur));
    }
  }

  if (info == null) {
    return (
      <div className="text-sm text-ink-muted">Loading About…</div>
    );
  }

  const checkLabel = CHECK_LABELS[check.kind];

  return (
    <div>
      {/* Brand hero — mirrors the centered logo + tagline treatment on
          sayzo.app's login page so the desktop app feels like one product
          with the web. Left-aligned here (matches the rest of the Settings
          panes), but visually prominent as the splash for this view. */}
      <section className="flex items-center gap-5 rounded-2xl border border-ink-border bg-gradient-to-br from-white to-blue-50/40 p-6">
        <img
          src={logoUrl}
          alt=""
          className="h-20 w-20 shrink-0 drop-shadow-sm"
        />
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight text-ink">
            Sayzo
          </h1>
          <p className="mt-1.5 max-w-sm text-sm leading-relaxed text-ink-muted">
            Your English coach, tuned to how you actually speak. Sayzo turns
            your real conversations into personalized coaching.
          </p>
        </div>
      </section>

      {/* Version + update-check */}
      <section className="mt-8">
        <div className="flex items-center gap-4">
          <KvLabel>Version</KvLabel>
          <div className="text-sm text-ink">{info.version}</div>
          <Button
            variant="secondary"
            onClick={handleCheck}
            disabled={
              check.kind === "checking" ||
              check.kind === "downloading" ||
              check.kind === "applying"
            }
          >
            {checkLabel}
          </Button>
        </div>
        <div className="mt-3 ml-[136px] text-sm">
          {check.kind === "checking" && (
            <span className="text-ink-muted">Checking…</span>
          )}
          {check.kind === "latest" && (
            <span className="text-ink-muted">
              ✓ You're on the latest version.
            </span>
          )}
          {check.kind === "available" && (
            <div className="space-y-2">
              <div className="text-ink">
                Version {check.version} is available.
              </div>
              <Button
                variant="primary"
                onClick={() => handleInstall(check.version)}
              >
                Install Sayzo {check.version}
              </Button>
            </div>
          )}
          {check.kind === "downloading" && (
            <div className="space-y-2">
              <div className="text-ink">
                Downloading Sayzo {check.version}…
              </div>
              {/* Simple inline progress bar — keeps the dependency graph
                  shallow (no chart lib for a 1-D percent). The width is
                  clamped 0–100 so a buggy event with percent>100 doesn't
                  blow out the layout. */}
              <div className="h-1.5 w-64 overflow-hidden rounded-full bg-ink-border">
                <div
                  className="h-full bg-ink transition-all duration-200"
                  style={{
                    width: `${Math.max(0, Math.min(100, check.percent))}%`,
                  }}
                />
              </div>
              <div className="text-xs text-ink-muted">
                {check.percent}%
              </div>
            </div>
          )}
          {check.kind === "applying" && (
            <div className="space-y-1">
              <div className="text-ink">
                Installing Sayzo {check.version}…
              </div>
              <div className="text-xs text-ink-muted">
                Sayzo will restart in a moment. This window will close.
              </div>
            </div>
          )}
          {check.kind === "queued" && (
            <div className="space-y-1">
              <div className="text-ink">
                Version {check.version} is ready to install.
              </div>
              <div className="text-xs text-ink-muted">
                It'll be applied the next time Sayzo starts.
              </div>
            </div>
          )}
          {check.kind === "error" && (
            <span className="text-ink-muted">
              {check.message || "Couldn't check right now. Please try again."}
            </span>
          )}
        </div>
      </section>

      <Divider />

      {/* Captures */}
      <PathRow
        label="Captures"
        path={info.captures_dir}
        actions={
          <Button
            variant="secondary"
            onClick={() => settingsBridge.openCapturesFolder()}
          >
            Open captures folder
          </Button>
        }
      />

      {/* Logs */}
      <PathRow
        label="Logs"
        path={info.logs_dir}
        actions={
          <>
            <Button
              variant="secondary"
              onClick={() => settingsBridge.openLogsFolder()}
            >
              Open logs folder
            </Button>
            <Button variant="secondary" onClick={handleCopyDiagnostics}>
              {copied ? "Copied!" : "Copy diagnostics"}
            </Button>
          </>
        }
      />

      <Divider />

      {/* Diagnostics (opt-out) — default ON, disclosed here + in onboarding.
          Gates the inventory headers + log upload (see diagnostics.py). The
          live agent picks the toggle up over IPC without a restart. */}
      <section className="mt-2">
        <div className="flex items-start gap-4">
          <KvLabel>Diagnostics</KvLabel>
          <div className="flex-1">
            <label className="flex cursor-pointer items-start justify-between gap-4">
              <div className="flex-1">
                <div className="text-sm text-ink">
                  Share anonymous diagnostics
                </div>
                <div className="mt-0.5 text-xs leading-snug text-ink-muted">
                  Sends your device OS, app version, and error logs to the
                  Sayzo team so we can find and fix problems faster. Never
                  includes meeting audio or transcripts.
                </div>
              </div>
              <Switch
                checked={info.share_diagnostics}
                onChange={(v) => void handleShareDiagnostics(v)}
                ariaLabel="Share anonymous diagnostics"
              />
            </label>
            <button
              type="button"
              className="mt-2 text-xs text-ink-muted underline underline-offset-2 hover:text-ink"
              onClick={() => settingsBridge.openUrl(info.privacy_url)}
            >
              Privacy policy
            </button>
          </div>
        </div>
      </section>

      <Divider />

      {/* Footer */}
      <section className="flex flex-wrap gap-3">
        <Button
          variant="secondary"
          onClick={() => settingsBridge.openUrl(info.web_app_url)}
        >
          Open web app
        </Button>
        <Button
          variant="secondary"
          onClick={() => settingsBridge.openUrl(info.support_url)}
        >
          Report an issue
        </Button>
      </section>
    </div>
  );
}

function KvLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="w-32 shrink-0 text-sm text-ink-muted">{children}</div>
  );
}

function Divider() {
  return <div className="my-8 h-px bg-ink-border" />;
}

interface PathRowProps {
  label: string;
  path: string;
  actions: React.ReactNode;
}

function PathRow({ label, path, actions }: PathRowProps) {
  return (
    <section className="mt-2">
      <div className="flex items-start gap-4">
        <KvLabel>{label}</KvLabel>
        <div className="flex-1">
          <div className="break-all text-xs text-ink">{path}</div>
          <div className="mt-2 flex flex-wrap gap-2">{actions}</div>
        </div>
      </div>
    </section>
  );
}
