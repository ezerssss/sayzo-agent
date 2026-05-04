import { useEffect, useRef, useState } from "react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge, AccountState, FetchStatus } from "../lib/bridge";

interface Props {
  initialAccountState: AccountState;
  onAccountReady: () => void;
  onClose: () => void;
}

const RECHECK_INTERVAL_MS = 8_000;

type BlockingState = Exclude<AccountState, "ok" | "unknown">;

// Copy per blocking state. The most common path is `onboarding_required`
// (signed in, never visited sayzo.app to finish).
const COPY: Record<
  BlockingState,
  { title: string; subtitle: string; cta: string }
> = {
  onboarding_required: {
    title: "Finish setting up at sayzo.app",
    subtitle:
      "Sayzo needs your account to be ready before it can coach you. Head to the web app to finish onboarding — it only takes about a minute.",
    cta: "Open sayzo.app to finish",
  },
  suspended: {
    title: "Your Sayzo account is paused",
    subtitle:
      "Visit sayzo.app to reactivate your account. Once it's active, come back here and click \"Check now\".",
    cta: "Manage your account",
  },
  deleted: {
    title: "This account has been removed",
    subtitle:
      "Your Sayzo account no longer exists. Sign in with a different account, or visit sayzo.app for help.",
    cta: "Open sayzo.app",
  },
};

// App.tsx only routes blocked states here, but a stale state racing with
// the auto-poll can briefly land us on "ok" or "unknown" — fall back to
// the most common copy rather than crashing on the lookup.
function asBlocking(state: AccountState): BlockingState {
  return state === "ok" || state === "unknown" ? "onboarding_required" : state;
}

export function FinishSignup({
  initialAccountState,
  onAccountReady,
  onClose,
}: Props) {
  const [accountState, setAccountState] =
    useState<AccountState>(initialAccountState);
  const copy = COPY[asBlocking(accountState)];

  const [opening, setOpening] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [openedUrl, setOpenedUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlightRecheck = useRef(false);

  // Auto-poll while the screen is mounted. The user might finish onboarding
  // in another tab and we want to advance without making them click. Paused
  // while a manual recheck is in flight so we don't double-fire.
  useEffect(() => {
    let cancelled = false;
    const id = window.setInterval(async () => {
      if (cancelled || inFlightRecheck.current) return;
      await runRecheck({ silent: true });
    }, RECHECK_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runRecheck(opts: { silent?: boolean } = {}): Promise<void> {
    if (inFlightRecheck.current) return;
    inFlightRecheck.current = true;
    if (!opts.silent) {
      setRechecking(true);
      setError(null);
    }
    try {
      const resp = await bridge.recheckAccountStatus();
      const next = resp.status.account_state;
      setAccountState(next);
      if (next === "ok") {
        onAccountReady();
        return;
      }
      if (!opts.silent) {
        setError(messageForFetchStatus(resp.fetch_status, resp.error));
      }
    } catch (e) {
      if (!opts.silent) {
        setError(String(e));
      }
    } finally {
      inFlightRecheck.current = false;
      if (!opts.silent) setRechecking(false);
    }
  }

  async function handleOpen() {
    setOpening(true);
    setError(null);
    try {
      const result = await bridge.openOnboardingUrl();
      if (result.opened) {
        setOpenedUrl(result.url);
      } else {
        setError(
          result.url
            ? `Couldn't open your browser. Visit ${result.url} manually.`
            : "Couldn't open the onboarding page. Try again in a moment.",
        );
        if (result.url) setOpenedUrl(result.url);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setOpening(false);
    }
  }

  return (
    <Layout
      title={copy.title}
      subtitle={copy.subtitle}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button onClick={handleOpen} disabled={opening}>
            {opening ? "Opening…" : copy.cta}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-2 text-xs text-ink-muted">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
          <span>
            We'll check automatically.{" "}
            <button
              type="button"
              onClick={() => void runRecheck()}
              disabled={rechecking || opening}
              className="underline hover:text-ink disabled:opacity-50"
            >
              {rechecking ? "Checking…" : "Check now"}
            </button>
          </span>
        </div>

        {openedUrl && (
          <div className="rounded-md border border-ink-border bg-gray-50 p-3 text-xs text-ink-muted">
            Opened <span className="font-mono text-ink">{openedUrl}</span> in
            your browser. Once you're done, this window will move on.
          </div>
        )}

        <p className="text-xs leading-relaxed text-ink-muted">
          You can close this window any time. Re-open Sayzo from your
          {" "}
          {navigator.platform.toLowerCase().includes("mac")
            ? "Applications folder"
            : "Start menu or desktop shortcut"}
          {" "}
          once you're done — we'll pick up right here.
        </p>

        {error && (
          <Alert>
            <div>
              <strong>Heads up.</strong> {error}
            </div>
          </Alert>
        )}
      </div>
    </Layout>
  );
}

function messageForFetchStatus(
  fetchStatus: FetchStatus,
  serverError: string | null,
): string | null {
  switch (fetchStatus) {
    case "ok":
    case "onboarding_required":
    case "suspended":
    case "deleted":
      // No error to show — the screen reflects the new state.
      return null;
    case "auth_required":
      return "Your sign-in expired. Close this window and reopen Sayzo to sign in again.";
    case "transient_error":
      return "Couldn't reach sayzo.app. Check your internet connection and try again.";
    case "unknown_error":
    default:
      return serverError
        ? `Something went wrong: ${serverError}`
        : "Something went wrong. Try again in a moment.";
  }
}
