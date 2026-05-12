// Typed bridge between the HUD React app and the Python HudBridge.
//
// Python → JS: the Python launcher writes newline-delimited JSON to the
// subprocess's stdin; window.py forwards each line into the webview via
// `window.evaluate_js("window.hudBridge.dispatch(<payload>)")`. The
// `dispatch` method routes the payload to every subscribed listener.
//
// JS → Python: subscribers call `hudBridge.sendEvent({...})`, which routes
// through `window.pywebview.api.hud_event(payload)`. The Python bridge
// writes the payload back to the parent process via stdout. The parent's
// stdout reader thread resolves any pending `ask_consent` future and
// fires pill / toast callbacks.
//
// Mock mode (Vite dev: VITE_USE_MOCK_BRIDGE=1) installs a `hud_event` stub
// on `window.pywebview.api` so this module works in a plain browser. See
// `mock-bridge-init.ts`.

export type ReasonKind = "hotkey" | "whitelist" | "manual";

export interface ShowPillCmd {
  cmd: "show_pill";
  reason: ReasonKind;
  reason_label: string;
  start_ts: number;
  hotkey: string;
}

export interface HidePillCmd {
  cmd: "hide_pill";
}

export interface SetPillCollapsedCmd {
  cmd: "set_pill_collapsed";
  collapsed: boolean;
}

export interface SetAudioLevelsCmd {
  cmd: "set_audio_levels";
  mic: number;
  system: number;
}

export interface ShowCardCmd {
  cmd: "show_card";
  request_id: string;
  title: string;
  body: string;
  yes_label: string;
  no_label: string;
  timeout_secs: number;
}

export interface ShowToastCmd {
  cmd: "show_toast";
  id: string;
  title: string;
  body: string;
  ttl_secs: number;
}

export interface ShowActionableCmd {
  cmd: "show_actionable";
  request_id: string;
  title: string;
  body: string;
  button_label: string;
  expire_after_secs: number;
}

export interface HideAllCmd {
  cmd: "hide_all";
}

export interface DemoModeCmd {
  cmd: "demo_mode";
  on: boolean;
}

export type HudCommand =
  | ShowPillCmd
  | HidePillCmd
  | SetPillCollapsedCmd
  | SetAudioLevelsCmd
  | ShowCardCmd
  | ShowToastCmd
  | ShowActionableCmd
  | HideAllCmd
  | DemoModeCmd;

export type HudEvent =
  | { event: "hud_ready" }
  | { event: "card_response"; request_id: string; answer: "yes" | "no" | "timeout" }
  | { event: "actionable_response"; request_id: string; outcome: "pressed" | "expired" }
  | { event: "pill_stop_clicked" }
  | { event: "pill_collapsed" }
  | { event: "pill_expanded" }
  | { event: "log"; level: "info" | "warning" | "error"; msg: string };

type Subscriber = (cmd: HudCommand) => void;

class HudBridge {
  private subscribers = new Set<Subscriber>();
  private queue: HudCommand[] = [];
  private readyEmitted = false;

  dispatch(cmd: HudCommand): void {
    if (this.subscribers.size === 0) {
      this.queue.push(cmd);
      return;
    }
    for (const cb of this.subscribers) {
      try {
        cb(cmd);
      } catch (e) {
        console.error("[hud-bridge] subscriber threw", e);
      }
    }
  }

  subscribe(cb: Subscriber): () => void {
    this.subscribers.add(cb);
    if (this.queue.length > 0) {
      const drained = this.queue;
      this.queue = [];
      for (const cmd of drained) {
        try {
          cb(cmd);
        } catch (e) {
          console.error("[hud-bridge] subscriber threw on drain", e);
        }
      }
    }
    return () => {
      this.subscribers.delete(cb);
    };
  }

  async sendEvent(event: HudEvent): Promise<void> {
    const api = (window as unknown as {
      pywebview?: { api?: { hud_event?: (payload: unknown) => Promise<unknown> } };
    }).pywebview?.api;
    if (!api || typeof api.hud_event !== "function") {
      console.debug("[hud-bridge] no pywebview api — dropping event", event);
      return;
    }
    try {
      await api.hud_event(event);
    } catch (e) {
      console.warn("[hud-bridge] hud_event call failed", e);
    }
  }

  markReadyOnce(): void {
    if (this.readyEmitted) return;
    this.readyEmitted = true;
    void this.sendEvent({ event: "hud_ready" });
  }
}

export const hudBridge = new HudBridge();

declare global {
  interface Window {
    hudBridge: HudBridge;
  }
}

(window as unknown as { hudBridge: HudBridge }).hudBridge = hudBridge;
