// Receives events pushed from the Python Bridge via
// `window.evaluate_js("window.sayzoEvents.push(...)")`.
//
// Python pushes objects shaped like:
//   { type: "login_url", url: "https://…/authorize?…" }
//   { type: "login_tick", seconds_remaining: 42 }
//   { type: "login_done" }
//   { type: "login_error", message: "..." }
//   { type: "login_cancelled" }

export type SayzoEvent =
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
  // Settings — About pane "Install update" flow (Stage 4).
  // Phases the install worker walks through, in order:
  //   - downloading: stream-fetch in progress; `percent` 0..99.
  //   - applying: download done + hash verified, agent quit triggered.
  //                The Settings window will close shortly as the agent
  //                tears down to run the installer / swap helper.
  //   - noop_already_latest: manifest says nothing newer to install.
  //   - queued_for_restart: stage written but agent not reachable; the
  //                boot-time apply path will pick it up next launch.
  //   - error: anything went wrong; `message` is user-safe.
  | {
      type: "update_phase";
      phase:
        | "downloading"
        | "applying"
        | "noop_already_latest"
        | "queued_for_restart"
        | "error";
      version?: string;
      percent?: number;
      message?: string;
    }
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
