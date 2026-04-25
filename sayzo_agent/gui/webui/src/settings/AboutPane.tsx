import { useEffect, useState } from "react";
import { settingsBridge, AboutInfo } from "../lib/settings-bridge";
import { subscribe, SayzoEvent } from "../lib/events";
import { Button } from "../components/ui/Button";

type CheckState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "latest" }
  | { kind: "available"; version: string; url: string }
  | { kind: "error" };

export function AboutPane() {
  const [info, setInfo] = useState<AboutInfo | null>(null);
  const [check, setCheck] = useState<CheckState>({ kind: "idle" });
  const [copied, setCopied] = useState(false);

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
        setCheck({ kind: "error" });
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

  async function handleCopyDiagnostics() {
    try {
      const { text } = await settingsBridge.getDiagnostics();
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can fail in obscure pywebview backend states; the
      // user can still re-trigger from the menu, so swallow silently.
    }
  }

  if (info == null) {
    return (
      <div className="text-sm text-ink-muted">Loading About…</div>
    );
  }

  const checkLabel =
    check.kind === "checking"
      ? "Checking…"
      : check.kind === "latest" || check.kind === "available"
        ? "Check again"
        : check.kind === "error"
          ? "Try again"
          : "Check for updates";

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        About Sayzo
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Sayzo listens only when you say so — it captures meetings on your
        machine and turns them into personalized speaking drills in the Sayzo
        web app.
      </p>

      {/* Version + update-check */}
      <section className="mt-8">
        <div className="flex items-center gap-4">
          <KvLabel>Version</KvLabel>
          <div className="text-sm text-ink">{info.version}</div>
          <Button
            variant="secondary"
            onClick={handleCheck}
            disabled={check.kind === "checking"}
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
                onClick={() => settingsBridge.openUrl(check.url)}
              >
                Download Sayzo {check.version}
              </Button>
            </div>
          )}
          {check.kind === "error" && (
            <span className="text-ink-muted">
              Couldn't check right now. Please try again.
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
