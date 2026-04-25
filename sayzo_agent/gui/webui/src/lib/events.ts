// Receives events pushed from the Python Bridge via
// `window.evaluate_js("window.sayzoEvents.push(...)")`.
//
// Python pushes objects shaped like:
//   { type: "download_progress", done: 12345, total: 67890 }
//   { type: "download_done", path: "/path/to/file" }
//   { type: "download_error", message: "..." }
//   { type: "login_url", url: "https://…/authorize?…" }
//   { type: "login_tick", seconds_remaining: 42 }
//   { type: "login_done" }
//   { type: "login_error", message: "..." }
//   { type: "login_cancelled" }

export type SayzoEvent =
  | { type: "download_progress"; done: number; total: number }
  | { type: "download_done"; path: string }
  | { type: "download_error"; message: string }
  | { type: "login_url"; url: string }
  | { type: "login_tick"; seconds_remaining: number }
  | { type: "login_done" }
  | { type: "login_error"; message: string }
  | { type: "login_cancelled" }
  // Settings — About pane update check.
  | {
      type: "update_result";
      has_update: boolean;
      version?: string;
      url?: string;
      notes?: string;
    }
  | { type: "update_error"; message: string }
  | { type: "status_updated"; status: unknown }; // future-proof

type Listener = (evt: SayzoEvent) => void;

const listeners = new Set<Listener>();

declare global {
  interface Window {
    sayzoEvents: { push(evt: SayzoEvent): void };
  }
}

// Install the global queue on module load. Python's evaluate_js calls land
// here; we fan out to every registered React listener.
window.sayzoEvents = {
  push(evt) {
    listeners.forEach((l) => {
      try {
        l(evt);
      } catch (e) {
        console.error("sayzoEvents listener threw:", e);
      }
    });
  },
};

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
