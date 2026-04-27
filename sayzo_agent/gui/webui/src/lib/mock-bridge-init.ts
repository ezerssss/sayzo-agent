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
// Importing this for its side effect is the cheapest way to ensure it runs
// before anything else imports `whenReady` from `bridge.ts`.

const MOCK_ON = import.meta.env.VITE_USE_MOCK_BRIDGE === "1";

if (MOCK_ON && typeof window !== "undefined") {
  // Stub api: any property access returns an async function that resolves
  // to `undefined`. Logged so we can see which methods would be hit.
  const stubApi = new Proxy(
    {},
    {
      get: (_target, prop) => {
        if (prop === "then") return undefined; // not a thenable
        return (...args: unknown[]) => {
          console.debug(
            `[mock-bridge] window.pywebview.api.${String(prop)}`,
            ...args,
          );
          return Promise.resolve(undefined);
        };
      },
    },
  );
  // Pretend the bridge is fully bound. `whenReady()` polls for *any*
  // function on `window.pywebview.api` — the Proxy returns one for every
  // key, so the readiness check passes instantly.
  (window as unknown as Record<string, unknown>).pywebview = { api: stubApi };

  // Default to the Settings shell on the Captures pane so `dev:mock`
  // lands directly on what we're previewing. If the user has explicitly
  // typed a hash, leave it alone.
  if (!window.location.hash || window.location.hash === "#") {
    window.location.hash = "#route=settings&pane=Captures";
  }
}

export {};
