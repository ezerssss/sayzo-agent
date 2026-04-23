import { useEffect, useRef, useState } from "react";
import { Button } from "../components/ui/Button";
import { Alert } from "../components/ui/Alert";
import { Layout } from "../components/Layout";
import { bridge } from "../lib/bridge";
import { subscribe, SayzoEvent } from "../lib/events";

interface Props {
  onSignedIn: () => void;
  onCancel: () => void;
}

type UiState = "idle" | "pending" | "error";

export function Welcome({ onSignedIn, onCancel }: Props) {
  const [uiState, setUiState] = useState<UiState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [secondsRemaining, setSecondsRemaining] = useState<number | null>(null);
  const [showCopyUrl, setShowCopyUrl] = useState(false);
  const [copied, setCopied] = useState(false);
  const urlInputRef = useRef<HTMLInputElement | null>(null);

  // Mirror unused prop so TS doesn't complain — App.tsx listens globally for
  // login_done and advances on its own.
  void onSignedIn;

  useEffect(() => {
    return subscribe((evt: SayzoEvent) => {
      if (evt.type === "login_url") {
        setLoginUrl(evt.url);
      } else if (evt.type === "login_tick") {
        setSecondsRemaining(evt.seconds_remaining);
      } else if (evt.type === "login_cancelled") {
        // User (or a superseding start_login) cancelled. Reset to idle.
        setUiState("idle");
        setSecondsRemaining(null);
        setShowCopyUrl(false);
        setCopied(false);
      } else if (evt.type === "login_error") {
        setError(evt.message);
        setUiState("error");
        setSecondsRemaining(null);
      } else if (evt.type === "login_done") {
        // App.tsx advances the screen — nothing to do locally except
        // ensure our state is clean in case the component isn't
        // unmounted yet.
        setUiState("idle");
        setSecondsRemaining(null);
      }
    });
  }, []);

  async function handleSignIn() {
    setError(null);
    setLoginUrl(null);
    setSecondsRemaining(null);
    setShowCopyUrl(false);
    setCopied(false);
    setUiState("pending");
    try {
      await bridge.startLogin();
    } catch (e) {
      setError(String(e));
      setUiState("error");
    }
  }

  async function handleCancelLogin() {
    try {
      await bridge.cancelLogin();
    } catch (e) {
      // Cancel is best-effort; surface the error but still go back to idle.
      console.warn("cancelLogin failed:", e);
      setUiState("idle");
      setSecondsRemaining(null);
    }
    // The worker will emit login_cancelled; the subscription above handles
    // the state reset. We fall through without changing state here to avoid
    // racing with the event.
  }

  async function handleCopyUrl() {
    if (!loginUrl) return;
    try {
      await navigator.clipboard.writeText(loginUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API can fail in some webview contexts. Fall back to
      // selecting the input so the user can Cmd+C manually.
      urlInputRef.current?.select();
    }
  }

  const isPending = uiState === "pending";
  const isError = uiState === "error";

  const footer = (
    <>
      {isPending ? (
        <Button variant="secondary" onClick={handleCancelLogin}>
          Cancel
        </Button>
      ) : (
        <Button variant="ghost" onClick={onCancel}>
          Quit
        </Button>
      )}
      {!isPending && (
        <Button onClick={handleSignIn} disabled={isPending}>
          {isError ? "Try again" : "Sign in"}
        </Button>
      )}
    </>
  );

  return (
    <Layout
      step="01"
      title="Welcome to Sayzo"
      subtitle="A quick two-minute setup. Nothing records until you say so."
      footer={footer}
    >
      <p className="text-sm leading-relaxed text-ink-muted">
        Sayzo syncs your captures to the webapp and unlocks personalized
        speaking drills — signing in is required to use Sayzo.
      </p>

      {uiState === "idle" && (
        <p className="mt-4 text-xs leading-relaxed text-ink-muted">
          Prefer not to sign in right now? Click <strong>Quit</strong>. You
          can reopen Sayzo Agent from your Applications folder anytime to
          finish signing in.
        </p>
      )}

      {isPending && (
        <div className="mt-6 space-y-4">
          <div className="flex items-center gap-3 rounded-md border border-ink-border bg-gray-50 p-3 text-sm">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
            <div>
              Waiting for sign-in in your browser
              {secondsRemaining !== null && secondsRemaining > 0 && (
                <span className="text-ink-muted">
                  {" "}
                  ({secondsRemaining}s left)
                </span>
              )}
              …
            </div>
          </div>
          <div>
            <button
              type="button"
              onClick={() => setShowCopyUrl((v) => !v)}
              className="text-xs text-ink-muted underline hover:text-ink"
            >
              Having trouble? Copy the sign-in URL
            </button>
            {showCopyUrl && (
              <div className="mt-2 space-y-2">
                <p className="text-xs text-ink-muted">
                  Paste this into any browser to finish signing in.
                </p>
                <div className="flex gap-2">
                  <input
                    ref={urlInputRef}
                    readOnly
                    value={loginUrl ?? ""}
                    className="flex-1 rounded-md border border-ink-border bg-white px-2 py-1 text-xs font-mono"
                    onFocus={(e) => e.currentTarget.select()}
                  />
                  <Button
                    variant="secondary"
                    onClick={handleCopyUrl}
                    disabled={!loginUrl}
                  >
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {isError && error && (
        <Alert className="mt-6">
          <div>
            <strong>Sign-in failed.</strong> {error}
          </div>
        </Alert>
      )}
    </Layout>
  );
}
