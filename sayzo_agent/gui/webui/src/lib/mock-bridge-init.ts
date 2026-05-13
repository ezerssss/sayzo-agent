// When `npm run dev:mock` is active (`VITE_USE_MOCK_BRIDGE=1` from
// `.env.mock`), the Python bridge isn't there because we're running in a
// plain browser. This module installs a stub `window.pywebview.api` so:
//
//   * `whenReady()` resolves immediately,
//   * any unmocked method call returns `Promise.resolve(undefined)` instead
//     of throwing,
//   * the user lands on the Captures pane by default (the whole point of
//     `dev:mock`).
//
// The Captures-specific methods override this stub via `settings-bridge.ts`
// before reaching `window.pywebview.api`, so they return realistic mock
// data. Other panes will mostly render in an empty/loading state — that's
// fine; this mode exists to design the Captures pane, not the whole app.
//
// Setup-flow preview: `#preview=finish-signup` (optionally
// `&state=onboarding_required|suspended|deleted`) routes the bundle into
// the setup `App` with mocked status/config that pin it on FinishSignup,
// so the layout can be tweaked in a browser without booting the agent.
//
// Importing this for its side effect is the cheapest way to ensure it runs
// before anything else imports `whenReady` from `bridge.ts`.

const MOCK_ON = import.meta.env.VITE_USE_MOCK_BRIDGE === "1";

type PreviewState = "onboarding_required" | "suspended" | "deleted";

function readHashParams(): URLSearchParams {
  const hash = window.location.hash.replace(/^#/, "");
  return new URLSearchParams(hash);
}

function readPreviewScreen(): string | null {
  return readHashParams().get("preview");
}

function readPreviewState(): PreviewState {
  const s = readHashParams().get("state");
  if (s === "suspended" || s === "deleted") return s;
  return "onboarding_required";
}

function platformGuess(): string {
  return navigator.platform.toLowerCase().includes("mac") ? "darwin" : "win32";
}

// Default proxy: every property is an async no-op that logs the call.
//
// `whenReady()` in `lib/bridge.ts` iterates the api with `for...in` and
// looks for a function — a Proxy over an empty `{}` reports zero own keys
// to `for...in` (the `get` trap doesn't synthesize keys for enumeration),
// so the probe would time out. Stash one real function on the target so
// `for...in` sees an enumerable function key and the probe passes.
function makeStubApi(
  overrides: Record<string, (...args: unknown[]) => Promise<unknown>>,
): object {
  const target: Record<string, (...args: unknown[]) => Promise<unknown>> = {
    __sayzo_mock_ready__: async () => undefined,
  };
  return new Proxy(target, {
    get: (t, prop) => {
      if (prop === "then") return undefined; // not a thenable
      const key = String(prop);
      if (key in overrides) return overrides[key];
      if (key in t) return t[key];
      return (...args: unknown[]) => {
        console.debug(`[mock-bridge] window.pywebview.api.${key}`, ...args);
        return Promise.resolve(undefined);
      };
    },
  });
}

function installFinishSignupPreviewBridge(): void {
  const state = readPreviewState();
  console.info(`[mock-bridge] FinishSignup preview — account_state=${state}`);

  const status = {
    has_token: true,
    has_mic_permission: null,
    has_permissions_onboarded: false,
    account_state: state,
    is_complete: false,
    resume_at: null,
  };

  const overrides: Record<string, (...args: unknown[]) => Promise<unknown>> = {
    get_status: async () => status,
    get_config_snapshot: async () => ({
      platform: platformGuess(),
      auth_url: "https://sayzo.app",
    }),
    get_hotkey: async () => ({ binding: "ctrl+alt+s", display: "Ctrl+Alt+S" }),
    recheck_account_status: async () => {
      // Stay in the same state so the screen doesn't auto-advance — this
      // is a layout preview, not a flow walkthrough.
      return {
        status,
        fetch_status: state,
        onboarding_url: "https://sayzo.app/onboarding",
        error: null,
      };
    },
    open_onboarding_url: async () => {
      console.info("[mock-bridge] open_onboarding_url (no-op in preview)");
      return { opened: true, url: "https://sayzo.app/onboarding" };
    },
    quit_app: async () => {
      console.info("[mock-bridge] quit_app — would close the window");
      return null;
    },
  };

  const stubApi = makeStubApi(overrides);
  (window as unknown as Record<string, unknown>).pywebview = { api: stubApi };
}

function installCapturesPreviewBridge(): void {
  // Stub api: any property access returns an async function that resolves
  // to `undefined`. Logged so we can see which methods would be hit.
  const stubApi = makeStubApi({});
  (window as unknown as Record<string, unknown>).pywebview = { api: stubApi };

  // Default to the Settings shell on the Captures pane so `dev:mock`
  // lands directly on what we're previewing. If the user has explicitly
  // typed a hash, leave it alone.
  if (!window.location.hash || window.location.hash === "#") {
    window.location.hash = "#route=settings&pane=Captures";
  }
}

function installHudPreviewBridge(): void {
  // The HUD subprocess switched to PySide6 + QWebChannel in v2.11. In
  // dev:hud (browser) mode there's no Qt host — we install a stub
  // ``window.__sayzoMockHudBridge`` global that the React-side
  // ``hud-bridge.ts`` recognises as a mock path (alongside the real
  // QWebChannel-based path used in production). Method calls just log
  // and resolve to null.
  console.info("[mock-bridge] HUD preview (Qt-style stub)");
  (window as unknown as Record<string, unknown>).__sayzoMockHudBridge = {
    hud_event: async (payload: unknown) => {
      console.info("[mock-bridge] hud_event", payload);
      return null;
    },
    set_window_visible: async (visible: unknown) => {
      console.info("[mock-bridge] set_window_visible", visible);
      return null;
    },
    set_window_size: async (w: unknown, h: unknown) => {
      console.info("[mock-bridge] set_window_size", w, h);
      return null;
    },
    start_system_move: async () => {
      // Browser tab has no host window to drag — log only.
      console.info("[mock-bridge] start_system_move");
      return null;
    },
  };

  // Force the HUD route (and any specific preview hint) into the hash if
  // not already there. Lets `npm run dev:hud` open the browser straight
  // onto the HUD without having to type the fragment by hand.
  const hash = window.location.hash.replace(/^#/, "");
  const params = new URLSearchParams(hash);
  if (params.get("route") !== "hud") {
    params.set("route", "hud");
  }
  // Enable the demo control strip by default in dev mock mode so the
  // user can click through each event type. Production startup never
  // hits this branch.
  if (!params.has("demo")) {
    params.set("demo", "1");
  }
  window.location.hash = params.toString();
}

if (MOCK_ON && typeof window !== "undefined") {
  const preview = readPreviewScreen();
  if (preview === "finish-signup") {
    installFinishSignupPreviewBridge();
  } else if (preview && preview.startsWith("hud-")) {
    installHudPreviewBridge();
  } else if (
    new URLSearchParams(window.location.hash.replace(/^#/, "")).get("route") ===
    "hud"
  ) {
    installHudPreviewBridge();
  } else {
    installCapturesPreviewBridge();
  }
}

export {};
