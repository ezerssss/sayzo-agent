// Typed bridge between the HUD React app and the Python HudBridge.
//
// v2.11+: the HUD subprocess runs on PySide6 + QtWebEngine instead of
// pywebview. The JS↔Python bridge moved from pywebview's `js_api`
// to Qt's `QWebChannel`. Three transports are supported:
//
//  1. Qt production: ``qt.webChannelTransport`` is injected by the host
//     before the page loads. We dynamically load Qt's bundled
//     ``qrc:///qtwebchannel/qwebchannel.js``, construct a QWebChannel,
//     and pull the ``hudPyBridge`` QObject off ``channel.objects``.
//     Each ``@Slot``-decorated Python method becomes a JS function on
//     that object that returns a Promise.
//
//  2. Dev mock (``VITE_USE_MOCK_BRIDGE=1``): ``mock-bridge-init.ts``
//     installs ``window.__sayzoMockHudBridge`` with stub methods.
//     We use it directly without going through QWebChannel.
//
//  3. No bridge: a built-in no-op fallback so callers don't crash if
//     neither transport is present (e.g. a stray browser session).
//
// Python → JS direction (commands like ``show_pill`` / ``show_card``)
// flows through ``window.hudBridge.dispatch(...)`` from
// ``HudWindow._evaluate_js_dispatch`` — same shape as before, just
// called via ``QWebEnginePage.runJavaScript`` instead of pywebview's
// ``evaluate_js``. ``dispatch`` and ``subscribe`` semantics are
// unchanged so ``HudApp.tsx`` doesn't need any updates.

export type ReasonKind = "hotkey" | "whitelist" | "manual";

export interface ShowPillCmd {
  cmd: "show_pill";
  reason: ReasonKind;
  reason_label: string;
  start_ts: number;
  hotkey: string;
  // Per-show identifier emitted as the request_id on the StatePill
  // mount-effect's `card_painted` event so the launcher can log
  // delta_ms. Optional so older agent builds that don't send it
  // still render the pill (the diagnostic just silently no-ops).
  paint_id?: string;
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
  // Optional "Snooze 1h"-style secondary button (v3.8.x). Absent on
  // single-button actionables; when present, ActionableToast renders a
  // second ghost button that emits outcome: "snoozed".
  secondary_button_label?: string;
}

// Post-capture coaching insight (v3.10+). A compact card fired by the
// CapturePoller once the server finishes analyzing a capture. Distinct
// from ShowActionableCmd because it carries a capture-source anchor and
// an optional verbatim quote; rendered by InsightCard, not ActionableToast.
// Outcomes reuse the actionable vocabulary: primary "See full feedback" →
// "pressed", secondary "Stop showing these" → "snoozed", dismiss/timeout →
// "expired".
export interface ShowInsightCmd {
  cmd: "show_insight";
  request_id: string;
  // Plain, self-explanatory one-liner (server-generated).
  headline: string;
  // The concrete suggestion / rewrite / observation.
  body: string;
  // "From your {source}" context line — agent-supplied from the local
  // record.json title, no server dependency.
  source_label: string;
  // Freshness chip text ("Just now" / "5 min ago" / "1 hr ago") — computed
  // by the agent at fire time from record.ended_at, so deferred fires
  // (user in another meeting when the insight became ready) don't claim
  // "Just now" for a capture that's actually nearly an hour old.
  freshness_label: string;
  // Verbatim quote from the user's own speech. Absent for insight types
  // that aren't about a specific utterance (strength / structure / pacing).
  quote?: string;
  // rephrase | structure | clarity | pacing | strength | other. Carried for
  // future per-type styling; not load-bearing for rendering today.
  insight_type?: string;
  button_label: string;
  expire_after_secs: number;
  // "Stop showing these" — the one-click off-switch. Always present in
  // production; optional so the demo path can omit it.
  secondary_button_label?: string;
}

export interface HideCardCmd {
  cmd: "hide_card";
  request_id: string;
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
  | ShowInsightCmd
  | HideCardCmd
  | HideAllCmd
  | DemoModeCmd;

export type HudEvent =
  | { event: "hud_ready" }
  | { event: "card_response"; request_id: string; answer: "yes" | "no" | "timeout" }
  | {
      event: "actionable_response";
      request_id: string;
      outcome: "pressed" | "expired" | "snoozed";
    }
  | {
      event: "insight_response";
      request_id: string;
      outcome: "pressed" | "expired" | "snoozed";
    }
  // Fired by each card / toast / insight component after one rAF in
  // its mount effect, so the parent agent can log the time delta
  // between "Python sent show_X" and "browser actually painted X
  // into the WebEngine GPU surface." Diagnoses the layered-window
  // paint-stall (window.py:319-326) — if delta_ms looks normal but
  // the user still doesn't see anything, the failure is in the Qt
  // → UpdateLayeredWindow compose path, not React.
  | { event: "card_painted"; request_id: string }
  | { event: "pill_stop_clicked" }
  | { event: "pill_collapsed" }
  | { event: "pill_expanded" }
  | { event: "log"; level: "info" | "warning" | "error"; msg: string };

type Subscriber = (cmd: HudCommand) => void;

// Internal transport abstraction. Both QWebChannel and the dev mock
// expose the same method shapes; the resolved object is used by the
// public HudBridge methods directly.
interface HudTransport {
  setWindowVisible(visible: boolean): Promise<void>;
  setWindowSize(width: number, height: number): Promise<void>;
  hudEvent(payloadJson: string): Promise<void>;
  startSystemMove(): Promise<void>;
}

interface QtBridgeObject {
  set_window_visible(visible: boolean): Promise<void>;
  set_window_size(width: number, height: number): Promise<void>;
  hud_event(payloadJson: string): Promise<void>;
  start_system_move(): Promise<void>;
}

interface QtWebChannelTransport {
  send(message: string): void;
  onmessage?: (event: { data: string }) => void;
}

declare global {
  interface Window {
    qt?: { webChannelTransport?: QtWebChannelTransport };
    QWebChannel?: new (
      transport: QtWebChannelTransport,
      callback: (channel: { objects: Record<string, QtBridgeObject> }) => void,
    ) => unknown;
    __sayzoMockHudBridge?: QtBridgeObject;
    hudBridge: HudBridge;
  }
}

function loadQWebChannelScript(): Promise<void> {
  if (window.QWebChannel) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(
      'script[data-sayzo-qwebchannel="1"]',
    );
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new Error("qwebchannel.js failed to load")),
        { once: true },
      );
      return;
    }
    const script = document.createElement("script");
    script.src = "qrc:///qtwebchannel/qwebchannel.js";
    script.dataset.sayzoQwebchannel = "1";
    script.onload = () => resolve();
    script.onerror = () =>
      reject(new Error("qwebchannel.js failed to load (qrc:///qtwebchannel/)"));
    document.head.appendChild(script);
  });
}

async function buildQtTransport(
  transport: QtWebChannelTransport,
): Promise<HudTransport> {
  await loadQWebChannelScript();
  const ChannelCtor = window.QWebChannel;
  if (!ChannelCtor) {
    throw new Error(
      "qwebchannel.js loaded but window.QWebChannel is undefined",
    );
  }
  return new Promise<HudTransport>((resolve, reject) => {
    let settled = false;
    const safetyId = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("QWebChannel handshake never completed after 10 s"));
    }, 10_000);
    try {
      // eslint-disable-next-line @typescript-eslint/no-unused-expressions
      new ChannelCtor(transport, (channel) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(safetyId);
        const py = channel.objects.hudPyBridge as QtBridgeObject | undefined;
        if (!py) {
          reject(new Error("QWebChannel ready but hudPyBridge not exposed"));
          return;
        }
        resolve({
          setWindowVisible: (v) => Promise.resolve(py.set_window_visible(v)),
          setWindowSize: (w, h) => Promise.resolve(py.set_window_size(w, h)),
          hudEvent: (json) => Promise.resolve(py.hud_event(json)),
          startSystemMove: () => Promise.resolve(py.start_system_move()),
        });
      });
    } catch (e) {
      if (settled) return;
      settled = true;
      window.clearTimeout(safetyId);
      reject(e);
    }
  });
}

// Bounded retry around the QWebChannel handshake. A single 10 s timeout
// then a permanent no-op was too brittle: a transient handshake stall (GPU
// init hitch on slow/old Macs) wedged the HUD for the whole session. Retry
// up to `attempts` times with a fresh handshake each, then give up — at
// which point the child-side ready watchdog (window.py, exit code 4) fires
// because `hud_ready` never landed, and the parent respawns the subprocess.
// JS can't signal Python here itself: with no transport, `hud_event` has
// nothing to ride.
async function buildQtTransportWithRetry(
  transport: QtWebChannelTransport,
  attempts = 3,
): Promise<HudTransport> {
  let lastErr: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await buildQtTransport(transport);
    } catch (e) {
      lastErr = e;
      console.warn(
        `[hud-bridge] QWebChannel handshake attempt ${i + 1}/${attempts} failed`,
        e,
      );
      if (i < attempts - 1) {
        await new Promise((r) => window.setTimeout(r, 1000));
      }
    }
  }
  console.error(
    `[hud-bridge] FATAL: no transport after ${attempts} attempts — HUD inert until parent respawn`,
    lastErr,
  );
  throw lastErr instanceof Error
    ? lastErr
    : new Error("QWebChannel handshake retries exhausted");
}

function buildMockTransport(stub: QtBridgeObject): HudTransport {
  return {
    setWindowVisible: (v) => Promise.resolve(stub.set_window_visible(v)),
    setWindowSize: (w, h) => Promise.resolve(stub.set_window_size(w, h)),
    hudEvent: (json) => Promise.resolve(stub.hud_event(json)),
    startSystemMove: () => Promise.resolve(stub.start_system_move()),
  };
}

function buildNoopTransport(reason: string): HudTransport {
  console.warn(`[hud-bridge] no transport available (${reason}) — using no-op`);
  return {
    setWindowVisible: async () => undefined,
    setWindowSize: async () => undefined,
    hudEvent: async () => undefined,
    startSystemMove: async () => undefined,
  };
}

// Detect the active backend on module load and return a Promise that
// resolves to its HudTransport. Order: mock > Qt > no-op. The mock
// short-circuits even in production because mock-bridge-init.ts only
// installs ``__sayzoMockHudBridge`` when ``VITE_USE_MOCK_BRIDGE=1``.
function awaitHudTransport(): Promise<HudTransport> {
  if (window.__sayzoMockHudBridge) {
    return Promise.resolve(buildMockTransport(window.__sayzoMockHudBridge));
  }
  const transport = window.qt?.webChannelTransport;
  if (transport) {
    return buildQtTransportWithRetry(transport).catch((e) => {
      console.warn("[hud-bridge] QWebChannel setup failed", e);
      return buildNoopTransport("QWebChannel setup error");
    });
  }
  // qt.webChannelTransport might still be landing — pywebview's
  // pywebviewready equivalent in Qt is the transport-injection step.
  // We poll for it on a short cadence with a 10 s deadline, then fall
  // back to no-op if it never appears.
  return new Promise<HudTransport>((resolve) => {
    const deadline = Date.now() + 10_000;
    const tick = () => {
      const t = window.qt?.webChannelTransport;
      if (t) {
        resolve(
          buildQtTransportWithRetry(t).catch((e) => {
            console.warn("[hud-bridge] QWebChannel setup failed (post-tick)", e);
            return buildNoopTransport("QWebChannel setup error");
          }),
        );
        return;
      }
      if (Date.now() > deadline) {
        resolve(buildNoopTransport("no qt.webChannelTransport after 10 s"));
        return;
      }
      window.setTimeout(tick, 50);
    };
    window.setTimeout(tick, 50);
  });
}

class HudBridge {
  private subscribers = new Set<Subscriber>();
  private queue: HudCommand[] = [];
  private readyEmitted = false;
  private transport: Promise<HudTransport> = awaitHudTransport();

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

  // All three JS→Python methods await the transport Promise. In the
  // typical case the transport is already resolved by the time React's
  // first useEffect fires (Qt's WebChannel transport is injected before
  // the page's scripts run; the mock is set up synchronously by
  // mock-bridge-init.ts). The Promise pattern guards against races
  // during cold boot without hand-rolled retry loops at call sites.

  async sendEvent(event: HudEvent): Promise<void> {
    try {
      const t = await this.transport;
      await t.hudEvent(JSON.stringify(event));
    } catch (e) {
      console.warn("[hud-bridge] hudEvent call failed", e);
    }
  }

  async setWindowVisible(visible: boolean): Promise<void> {
    try {
      const t = await this.transport;
      await t.setWindowVisible(visible);
    } catch (e) {
      console.warn("[hud-bridge] setWindowVisible call failed", e);
    }
  }

  async setWindowSize(width: number, height: number): Promise<void> {
    try {
      const t = await this.transport;
      await t.setWindowSize(width, height);
    } catch (e) {
      console.warn("[hud-bridge] setWindowSize call failed", e);
    }
  }

  async startSystemMove(): Promise<void> {
    try {
      const t = await this.transport;
      await t.startSystemMove();
    } catch (e) {
      console.warn("[hud-bridge] startSystemMove call failed", e);
    }
  }

  markReadyOnce(): void {
    if (this.readyEmitted) return;
    this.readyEmitted = true;
    void this.sendEvent({ event: "hud_ready" });
  }
}

export const hudBridge = new HudBridge();

(window as unknown as { hudBridge: HudBridge }).hudBridge = hudBridge;
