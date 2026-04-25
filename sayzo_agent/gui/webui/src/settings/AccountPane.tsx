import { useEffect, useState } from "react";
import { settingsBridge, AccountStatus } from "../lib/settings-bridge";
import { subscribe, SayzoEvent } from "../lib/events";
import { Button } from "../components/ui/Button";

// Local UI state for the signed-out branch — mirrors the tkinter Account
// pane's three states (idle / pending / error). When signed_in, none of
// these matter.
type SignInState =
  | { kind: "idle" }
  | { kind: "pending"; secondsRemaining: number | null; loginUrl: string | null }
  | { kind: "error"; message: string };

function formatSignedInSince(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(d);
}

export function AccountPane() {
  const [status, setStatus] = useState<AccountStatus | null>(null);
  const [ui, setUi] = useState<SignInState>({ kind: "idle" });
  const [copied, setCopied] = useState(false);

  async function refreshStatus() {
    try {
      const s = await settingsBridge.accountStatus();
      setStatus(s);
    } catch {
      // Treat as signed_out — most likely cause is a TokenStore read failure
      // and the user should be invited to sign in fresh.
      setStatus({ state: "signed_out" });
    }
  }

  useEffect(() => {
    void refreshStatus();
  }, []);

  useEffect(() => {
    return subscribe((evt: SayzoEvent) => {
      switch (evt.type) {
        case "login_url":
          setUi((cur) =>
            cur.kind === "pending"
              ? { ...cur, loginUrl: evt.url }
              : { kind: "pending", secondsRemaining: null, loginUrl: evt.url },
          );
          break;
        case "login_tick":
          setUi((cur) =>
            cur.kind === "pending"
              ? { ...cur, secondsRemaining: evt.seconds_remaining }
              : cur,
          );
          break;
        case "login_done":
          setUi({ kind: "idle" });
          void refreshStatus();
          break;
        case "login_error":
          setUi({ kind: "error", message: evt.message });
          break;
        case "login_cancelled":
          setUi({ kind: "idle" });
          break;
        default:
          break;
      }
    });
  }, []);

  async function handleSignIn() {
    setUi({ kind: "pending", secondsRemaining: null, loginUrl: null });
    try {
      await settingsBridge.startLogin();
    } catch (e) {
      setUi({ kind: "error", message: String(e) });
    }
  }

  async function handleCancel() {
    try {
      await settingsBridge.cancelLogin();
    } catch {
      // The Python side emits login_cancelled regardless; ignore round-trip
      // failure and let the event handler reset us.
    }
  }

  async function handleSignOut() {
    try {
      await settingsBridge.signOut();
    } finally {
      void refreshStatus();
    }
  }

  async function handleCopyUrl(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // No clipboard API → user can still see the URL; do nothing.
    }
  }

  if (status == null) {
    return <div className="text-sm text-ink-muted">Loading account…</div>;
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Account
      </h1>

      {status.state === "signed_in" ? (
        <SignedInBody status={status} onSignOut={handleSignOut} />
      ) : (
        <SignedOutBody
          ui={ui}
          copied={copied}
          onSignIn={handleSignIn}
          onCancel={handleCancel}
          onCopyUrl={handleCopyUrl}
          onTryAgain={handleSignIn}
        />
      )}
    </div>
  );
}

interface SignedInBodyProps {
  status: Extract<AccountStatus, { state: "signed_in" }>;
  onSignOut: () => void;
}

function SignedInBody({ status, onSignOut }: SignedInBodyProps) {
  const server = status.server || "—";
  return (
    <div>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Signed in. Your captures sync to your account so you can drill the
        coaching moments in the Sayzo web app.
      </p>

      <dl className="mt-8 space-y-2">
        <KvRow label="Server" value={server} />
        <KvRow
          label="Signed in since"
          value={formatSignedInSince(status.signed_in_since)}
        />
      </dl>

      <div className="my-8 h-px bg-ink-border" />

      <div className="flex flex-wrap gap-3">
        {status.server && (
          <Button
            variant="primary"
            onClick={() => settingsBridge.openUrl(status.server)}
          >
            Open web app
          </Button>
        )}
        <Button
          variant="secondary"
          className="border-red-200 text-red-700 hover:bg-red-50"
          onClick={onSignOut}
        >
          Sign out
        </Button>
      </div>
    </div>
  );
}

interface SignedOutBodyProps {
  ui: SignInState;
  copied: boolean;
  onSignIn: () => void;
  onCancel: () => void;
  onCopyUrl: (url: string) => void;
  onTryAgain: () => void;
}

function SignedOutBody({
  ui,
  copied,
  onSignIn,
  onCancel,
  onCopyUrl,
  onTryAgain,
}: SignedOutBodyProps) {
  if (ui.kind === "pending") {
    const secs = ui.secondsRemaining;
    return (
      <div>
        <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
          {secs != null && secs > 0
            ? `Waiting for sign-in in your browser… (${secs}s left)`
            : "Waiting for sign-in in your browser…"}
        </p>

        <div className="mt-6">
          <Button variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        </div>

        <p className="mt-8 max-w-md text-sm leading-relaxed text-ink-muted">
          Having trouble? Copy the sign-in URL and paste it into any browser
          to finish.
        </p>
        <div className="mt-3 flex max-w-md gap-2">
          <input
            type="text"
            readOnly
            value={ui.loginUrl ?? ""}
            placeholder="Waiting for URL…"
            className="flex-1 rounded-md border border-ink-border bg-white px-3 py-2 text-xs text-ink focus:border-accent focus:outline-none"
          />
          <Button
            variant="secondary"
            disabled={!ui.loginUrl}
            onClick={() => ui.loginUrl && onCopyUrl(ui.loginUrl)}
          >
            {copied ? "Copied!" : "Copy"}
          </Button>
        </div>
      </div>
    );
  }

  if (ui.kind === "error") {
    return (
      <div>
        <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
          Sign-in failed: {ui.message || "Unknown error."}
        </p>
        <div className="mt-6">
          <Button variant="primary" onClick={onTryAgain}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        You're not signed in. Sayzo will keep captures on this machine until
        you do — so no coaching drills yet.
      </p>
      <div className="mt-6">
        <Button variant="primary" onClick={onSignIn}>
          Sign in
        </Button>
      </div>
    </div>
  );
}

interface KvRowProps {
  label: string;
  value: string;
}

function KvRow({ label, value }: KvRowProps) {
  return (
    <div className="flex gap-4">
      <dt className="w-32 shrink-0 text-sm text-ink-muted">{label}</dt>
      <dd className="text-sm text-ink">{value}</dd>
    </div>
  );
}
