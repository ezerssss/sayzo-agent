import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Sparkles } from "lucide-react";
import { hudBridge, HudCommand } from "./lib/hud-bridge";
import { HudShell } from "./hud/HudShell";
import { StatePill } from "./hud/StatePill";
import { DotIndicator } from "./hud/DotIndicator";
import { ConsentCard } from "./hud/ConsentCard";
import { InfoToast } from "./hud/InfoToast";
import { ActionableToast } from "./hud/ActionableToast";
import { InsightCard } from "./hud/InsightCard";
import { HudCard } from "./hud/HudCard";

// Duration of the CSS opacity fade (see `index.css::.hud-fade`). When
// content goes away we keep the OS window on-screen for this long so
// the user sees the children fade out, then we tell Python to hide the
// host window. Must match the CSS transition or the window will hide
// mid-animation (still visible would-be content snapping to nothing) or
// be left on screen long after the animation finishes.
const HUD_FADE_MS = 180;

// Top-right HUD root. Owns the state machine: pill visibility / collapsed,
// queued consent cards (FIFO, one at a time), stacked toasts, the
// daily-drill actionable. Subscribes to incoming commands from Python
// via window.hudBridge.dispatch; emits responses via hudBridge.sendEvent.

interface PillState {
  reason: string;
  reasonLabel: string;
  startTs: number;
  hotkey: string;
  collapsed: boolean;
}

interface CardState {
  request_id: string;
  title: string;
  body: string;
  yes_label: string;
  no_label: string;
  timeout_secs: number;
}

interface ToastState {
  id: string;
  title: string;
  body: string;
  ttl_secs: number;
}

interface ActionableState {
  request_id: string;
  title: string;
  body: string;
  button_label: string;
  expire_after_secs: number;
  secondary_button_label?: string;
}

interface InsightState {
  request_id: string;
  headline: string;
  body: string;
  source_label: string;
  freshness_label: string;
  quote?: string;
  insight_type?: string;
  button_label: string;
  expire_after_secs: number;
  secondary_button_label?: string;
}

const MAX_VISIBLE_TOASTS = 3;

function previewLabelFor(kind: string): string {
  switch (kind) {
    case "hud-pill":
      return "show pill";
    case "hud-dot":
      return "collapse to dot";
    case "hud-card":
      return "fire consent card";
    case "hud-toast":
      return "fire info toast";
    case "hud-actionable":
      return "fire actionable";
    case "hud-insight":
      return "fire insight";
    case "hud-insight-deferred":
      return "fire insight (deferred)";
    default:
      return kind;
  }
}

export function HudApp() {
  const [pill, setPill] = useState<PillState | null>(null);
  const [cards, setCards] = useState<CardState[]>([]);
  const [toasts, setToasts] = useState<ToastState[]>([]);
  const [actionable, setActionable] = useState<ActionableState | null>(null);
  const [insight, setInsight] = useState<InsightState | null>(null);
  const [demoMode, setDemoMode] = useState(false);
  // Combined mic + system audio level (max of the two). `undefined`
  // means no real audio is streaming and the Waveform self-animates;
  // a defined value flips it into "react to real audio" mode.
  const [audioLevel, setAudioLevel] = useState<number | undefined>(undefined);

  // Force a re-render path for demo mode if the hash hints at it.
  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    if (params.get("demo") === "1") {
      setDemoMode(true);
    }
  }, []);

  // Opt this React app's body OUT of the default opaque white background
  // declared in index.css for Setup / Settings windows. The HUD is an
  // overlay on top of the user's desktop — it MUST be fully transparent
  // except for the actual React content it renders. Without this, an
  // empty HudShell painted a 24x24 white square on Windows and may have
  // been preventing macOS WindowServer from compositing the translucent
  // window at all. CSS rule lives at `body.hud-overlay` in index.css.
  useEffect(() => {
    document.body.classList.add("hud-overlay");
    return () => document.body.classList.remove("hud-overlay");
  }, []);

  // Pause-pill-during-consent at the React layer. Mirrors what
  // `ArmController._ask_consent_pausing_pill` does in production:
  // when a consent card appears while the pill is shown, hide the
  // pill for the duration of the card; restore it once the card is
  // dismissed. Necessary for the demo flow (where the demo buttons
  // fire `show_card` without explicitly hiding the pill first);
  // in production the ArmController already dispatches `hide_pill`
  // before `show_card` so this effect is a no-op because the pill
  // is already null when the card mounts.
  const [pausedPill, setPausedPill] = useState<PillState | null>(null);
  useEffect(() => {
    if (cards.length > 0 && pill && !pausedPill) {
      setPausedPill(pill);
      setPill(null);
      return;
    }
    if (cards.length === 0 && pausedPill) {
      setPill(pausedPill);
      setPausedPill(null);
    }
  }, [cards.length, pill, pausedPill]);

  // Delegate window dragging to Qt. `-webkit-app-region: drag` (the
  // CSS that worked in pywebview's Chromium host) is a no-op in
  // QtWebEngine, so we intercept the mousedown ourselves: if the user
  // clicks on a `.hud-drag` ancestor that isn't a button / input /
  // `.hud-no-drag` element, we ask Qt to begin a native window drag
  // via `QWindow.startSystemMove()`. The OS takes over from there —
  // cursor tracking, snapping, and release are all native.
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement | null;
      if (!target) return;
      // Single closest() walk finds the nearest ancestor matching any
      // of: drag-opt-out, interactive child, or drag region. Whichever
      // appears first up the tree wins, so opt-outs/interactives
      // automatically short-circuit before we'd ever hit a `.hud-drag`.
      const hit = target.closest(
        ".hud-no-drag, button, a, input, textarea, select, .hud-drag",
      );
      if (!hit) return;
      if (hit.matches(".hud-drag")) {
        e.preventDefault();
        void hudBridge.startSystemMove();
      }
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  // Report content size to Python so the Qt host window can resize
  // to *exactly* the rendered content rectangle. We observe the
  // HudShell element directly via a ref — NOT
  // ``document.documentElement.scrollWidth``, which is defined as
  // max(viewport, content) and therefore never shrinks below the
  // current Qt window's viewport. Without the direct measurement, a
  // pill→dot collapse leaves the documentElement reporting the old
  // (pill-sized) viewport, the window doesn't shrink, and the dot
  // ends up left-aligned inside a too-wide window. rAF-coalesces to
  // one resize per frame so transient flex layout passes don't fire
  // a flood of IPC calls.
  const shellRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    let rafId = 0;
    let lastW = 0;
    let lastH = 0;
    const flush = () => {
      rafId = 0;
      const rect = shell.getBoundingClientRect();
      const w = Math.ceil(rect.width);
      const h = Math.ceil(rect.height);
      if (w <= 0 || h <= 0) return;
      if (w === lastW && h === lastH) return;
      lastW = w;
      lastH = h;
      void hudBridge.setWindowSize(w, h);
    };
    const ro = new ResizeObserver(() => {
      if (rafId !== 0) return;
      rafId = requestAnimationFrame(flush);
    });
    ro.observe(shell);
    rafId = requestAnimationFrame(flush);
    return () => {
      if (rafId !== 0) cancelAnimationFrame(rafId);
      ro.disconnect();
    };
  }, []);

  // Subscribe to incoming commands from Python. The bridge buffers any
  // commands that arrive before the first subscriber attaches (e.g. cold
  // boot races where the launcher writes before React mounts), and
  // flushes them on subscribe.
  useEffect(() => {
    const unsub = hudBridge.subscribe((cmd: HudCommand) => {
      switch (cmd.cmd) {
        case "show_pill":
          setPill({
            reason: cmd.reason,
            reasonLabel: cmd.reason_label,
            startTs: cmd.start_ts,
            hotkey: cmd.hotkey,
            collapsed: false,
          });
          break;
        case "hide_pill":
          setPill(null);
          break;
        case "set_pill_collapsed":
          setPill((p) => (p ? { ...p, collapsed: cmd.collapsed } : p));
          break;
        case "set_audio_levels": {
          // mic / system arrive already per-source-normalized to [0, 1]
          // from the agent (see Agent._consume's slow-peak normalizer),
          // so a quiet mic and a loud Blue Yeti both fill the bars
          // similarly during speech. No per-source gain juggling here —
          // we just pick the louder of the two and let Waveform's dB
          // shaping handle perceptual feel.
          setAudioLevel(Math.max(0, Math.min(1, Math.max(cmd.mic, cmd.system))));
          break;
        }
        case "show_card":
          setCards((cs) => [
            ...cs,
            {
              request_id: cmd.request_id,
              title: cmd.title,
              body: cmd.body,
              yes_label: cmd.yes_label,
              no_label: cmd.no_label,
              timeout_secs: cmd.timeout_secs,
            },
          ]);
          break;
        case "show_toast":
          setToasts((ts) => {
            const next = [...ts, {
              id: cmd.id,
              title: cmd.title,
              body: cmd.body,
              ttl_secs: cmd.ttl_secs,
            }];
            // Evict oldest if we go over the visible cap.
            return next.slice(-MAX_VISIBLE_TOASTS);
          });
          break;
        case "show_actionable":
          setActionable({
            request_id: cmd.request_id,
            title: cmd.title,
            body: cmd.body,
            button_label: cmd.button_label,
            expire_after_secs: cmd.expire_after_secs,
            secondary_button_label: cmd.secondary_button_label,
          });
          break;
        case "show_insight":
          setInsight({
            request_id: cmd.request_id,
            headline: cmd.headline,
            body: cmd.body,
            source_label: cmd.source_label,
            freshness_label: cmd.freshness_label,
            quote: cmd.quote,
            insight_type: cmd.insight_type,
            button_label: cmd.button_label,
            expire_after_secs: cmd.expire_after_secs,
            secondary_button_label: cmd.secondary_button_label,
          });
          break;
        case "hide_all":
          setPill(null);
          setCards([]);
          setToasts([]);
          setActionable(null);
          setInsight(null);
          break;
        case "demo_mode":
          setDemoMode(cmd.on);
          break;
      }
    });

    // Signal readiness — the launcher's stdout reader unblocks any
    // buffered show_* commands when it sees this. Wrapped in a single
    // microtask so the subscriber above is registered before any
    // dispatch round-trip happens through evaluate_js.
    queueMicrotask(() => hudBridge.markReadyOnce());

    return unsub;
  }, []);

  const handleCardAnswer = useCallback(
    (request_id: string, answer: "yes" | "no" | "timeout") => {
      setCards((cs) => cs.filter((c) => c.request_id !== request_id));
      void hudBridge.sendEvent({
        event: "card_response",
        request_id,
        answer,
      });
    },
    [],
  );

  const handleToastExpire = useCallback((id: string) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
  }, []);

  const handleActionable = useCallback(
    (request_id: string, outcome: "pressed" | "expired" | "snoozed") => {
      setActionable(null);
      void hudBridge.sendEvent({
        event: "actionable_response",
        request_id,
        outcome,
      });
    },
    [],
  );

  const handleInsight = useCallback(
    (request_id: string, outcome: "pressed" | "expired" | "snoozed") => {
      setInsight(null);
      void hudBridge.sendEvent({
        event: "insight_response",
        request_id,
        outcome,
      });
    },
    [],
  );

  const handlePillStop = useCallback(() => {
    // Optimistic local hide — matches what production does anyway:
    // ArmController._on_pill_stop_clicked triggers _disarm_internal,
    // which calls launcher.hide_pill() to clear the pill. The agent's
    // own hide_pill round-trip lands shortly after; it's idempotent
    // (the pill is already gone). Without this optimistic clear the
    // demo preview (which has no launcher reading stdout) would leave
    // the pill on screen after stop, which is misleading.
    setPill(null);
    void hudBridge.sendEvent({ event: "pill_stop_clicked" });
  }, []);

  const handlePillCollapse = useCallback(() => {
    setPill((p) => (p ? { ...p, collapsed: true } : p));
    void hudBridge.sendEvent({ event: "pill_collapsed" });
  }, []);

  const handlePillExpand = useCallback(() => {
    setPill((p) => (p ? { ...p, collapsed: false } : p));
    void hudBridge.sendEvent({ event: "pill_expanded" });
  }, []);

  // Only the FIFO head card is visible. Queued cards wait their turn.
  const activeCard = cards[0] ?? null;

  // Anything-on-screen check. In production this drives the OS-level
  // hide/show of the host pywebview window — without it the HUD would
  // render as a permanent opaque rectangle in the top-right corner
  // (on Windows where transparency isn't viable through WebView2).
  // `demoMode` is included so the dev preview window stays visible
  // while the demo controls are up.
  const hasContent = useMemo(
    () =>
      !!pill ||
      cards.length > 0 ||
      toasts.length > 0 ||
      !!actionable ||
      !!insight ||
      demoMode,
    [pill, cards.length, toasts.length, actionable, insight, demoMode],
  );

  // Window visibility orchestration. Per-element CSS keyframes
  // (`hud-element-enter` in index.css) drive the IN animation for
  // each pill / card / toast as it mounts. The shell-level fade
  // (`hud-fade-out` class on `HudShell`) is the OUT path only: when
  // everything goes away we let the shell fade out before telling
  // Python to move the OS window offscreen, so the user sees a soft
  // dismissal rather than a snap.
  const [windowShown, setWindowShown] = useState(false);

  useEffect(() => {
    const callOs = (v: boolean) => {
      void hudBridge.setWindowVisible(v);
    };
    if (hasContent) {
      if (!windowShown) {
        setWindowShown(true);
        callOs(true);
      }
      return;
    }
    // hasContent went false — let the shell fade-out class run, then
    // hide the OS window after the fade settles.
    if (!windowShown) return;
    const t = window.setTimeout(() => {
      setWindowShown(false);
      callOs(false);
    }, HUD_FADE_MS);
    return () => window.clearTimeout(t);
  }, [hasContent, windowShown]);

  // Demo controls — dispatch synthetic commands into the bridge so the
  // exact same code path the production launcher uses also drives the
  // preview. No mock state, no parallel renderers.
  const demoActions = useMemo(
    () => [
      {
        key: "hud-pill",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_pill",
            reason: "hotkey",
            reason_label: "Hotkey",
            start_ts: Date.now() / 1000,
            hotkey: "Ctrl+Alt+S",
          }),
      },
      {
        key: "hud-pill-zoom",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_pill",
            reason: "whitelist",
            reason_label: "Zoom",
            start_ts: Date.now() / 1000 - 752,
            hotkey: "Ctrl+Alt+S",
          }),
      },
      {
        key: "hud-dot",
        run: () =>
          hudBridge.dispatch({ cmd: "set_pill_collapsed", collapsed: true }),
      },
      {
        key: "hud-card",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_card",
            request_id: `demo-${Date.now()}`,
            title: "Sayzo is ready to coach you",
            body: "Looks like you're in Zoom. Want us to capture this call for personalized speaking drills?",
            yes_label: "Start coaching",
            no_label: "Not now",
            timeout_secs: 15,
          }),
      },
      {
        key: "hud-card-end",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_card",
            request_id: `demo-${Date.now()}`,
            title: "Was that the end of your meeting?",
            body: "It's been quiet for a bit. Wrap up and save, or keep going?",
            yes_label: "Wrap up",
            no_label: "Keep going",
            timeout_secs: 12,
          }),
      },
      {
        key: "hud-toast",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_toast",
            id: `toast-${Date.now()}`,
            title: "Conversation saved",
            body: "Discussion about Q4 targets · 2m 34s",
            ttl_secs: 4,
          }),
      },
      {
        key: "hud-actionable",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_actionable",
            request_id: `actionable-${Date.now()}`,
            title: "Daily speaking drill",
            body: "Two minutes today — practice the filler-word habit you've been working on.",
            button_label: "Open drill",
            secondary_button_label: "Snooze 1h",
            expire_after_secs: 30,
          }),
      },
      {
        key: "hud-insight",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_insight",
            request_id: `insight-${Date.now()}`,
            // Matches what ``capture_poller._source_label`` produces in
            // production (12-hour wall clock + arm-app key). Iterate the
            // card against the SAME string users see — a shorter "Zoom
            // call" hid the wrap behavior the real label triggers.
            source_label: "2:30 pm Zoom call",
            // ``_freshness_label`` computes this at fire time from
            // record.ended_at. Demo it as "Just now" — preview the
            // common case; deferred fires read e.g. "12 min ago".
            freshness_label: "Just now",
            headline: "A clearer way to give your update",
            quote: "I think maybe we could possibly look into it?",
            body: "Try stating it directly: “I recommend we look into it.”",
            insight_type: "rephrase",
            button_label: "See full feedback",
            secondary_button_label: "Stop showing these",
            expire_after_secs: 120,
          }),
      },
      {
        key: "hud-insight-deferred",
        run: () =>
          hudBridge.dispatch({
            cmd: "show_insight",
            request_id: `insight-${Date.now()}`,
            source_label: "9:15 am call",
            // Preview what the chip looks like for a deferred fire —
            // the user was in another meeting for 23 min when this
            // capture's insight became ready.
            freshness_label: "23 min ago",
            headline: "Open with the point, then the context",
            quote: "Um, well, there were a few things going on this morning that I wanted to flag, but anyway, the deploy is blocked.",
            body: "Try leading: “The deploy is blocked — here's what happened this morning.”",
            insight_type: "structure",
            button_label: "See full feedback",
            secondary_button_label: "Stop showing these",
            expire_after_secs: 120,
          }),
      },
      {
        key: "hud-hide",
        run: () => hudBridge.dispatch({ cmd: "hide_all" }),
      },
    ],
    [],
  );

  // Auto-fire one preview command if the URL hints at a specific scenario.
  // Lets `npm run dev:hud-card` open straight to the consent card view
  // without manual interaction.
  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    const preview = params.get("preview");
    if (!preview) return;
    const match = demoActions.find((a) => a.key === preview);
    if (!match) return;
    // For card / actionable previews, also show the pill underneath so
    // the layered look matches production.
    if (preview === "hud-card" || preview === "hud-card-end" || preview === "hud-actionable") {
      hudBridge.dispatch({
        cmd: "show_pill",
        reason: "hotkey",
        reason_label: "Hotkey",
        start_ts: Date.now() / 1000 - 35,
        hotkey: "Ctrl+Alt+S",
      });
    }
    const id = setTimeout(() => match.run(), 100);
    return () => clearTimeout(id);
  }, [demoActions]);

  return (
    <HudShell ref={shellRef} visible={hasContent}>
      {/* Base layer: pill or dot, shown only while armed. */}
      {pill && !pill.collapsed && (
        <StatePill
          audioLevel={audioLevel}
          onStop={handlePillStop}
          onCollapse={handlePillCollapse}
        />
      )}
      {pill && pill.collapsed && (
        <DotIndicator onExpand={handlePillExpand} />
      )}

      {/* Toasts: stacked, oldest at top, newest at bottom. */}
      {toasts.map((t) => (
        <InfoToast
          key={t.id}
          title={t.title}
          body={t.body}
          ttlSecs={t.ttl_secs}
          onExpire={() => handleToastExpire(t.id)}
        />
      ))}

      {/* FIFO consent card. Only the head is rendered. */}
      {activeCard && (
        <ConsentCard
          key={activeCard.request_id}
          title={activeCard.title}
          body={activeCard.body}
          yesLabel={activeCard.yes_label}
          noLabel={activeCard.no_label}
          timeoutSecs={activeCard.timeout_secs}
          onAnswer={(a) => handleCardAnswer(activeCard.request_id, a)}
        />
      )}

      {/* Actionable (daily drill). */}
      {actionable && (
        <ActionableToast
          key={actionable.request_id}
          title={actionable.title}
          body={actionable.body}
          buttonLabel={actionable.button_label}
          secondaryButtonLabel={actionable.secondary_button_label}
          expireAfterSecs={actionable.expire_after_secs}
          onOutcome={(o) => handleActionable(actionable.request_id, o)}
        />
      )}

      {/* Post-capture coaching insight. */}
      {insight && (
        <InsightCard
          key={insight.request_id}
          headline={insight.headline}
          body={insight.body}
          sourceLabel={insight.source_label}
          freshnessLabel={insight.freshness_label}
          quote={insight.quote}
          buttonLabel={insight.button_label}
          secondaryButtonLabel={insight.secondary_button_label}
          expireAfterSecs={insight.expire_after_secs}
          onOutcome={(o) => handleInsight(insight.request_id, o)}
        />
      )}

      {/* Demo control strip. Visible only with #demo=1 or after the
          launcher sends the demo_mode command. Lets a developer click
          through each event type against the real frameless window. */}
      {demoMode && (
        <div className="mt-auto">
          <HudCard className="px-2 pb-2 pt-1">
            <div className="mt-1 flex items-center gap-1.5 px-1 text-[10px] uppercase tracking-wider text-ink-muted">
              <Sparkles size={11} />
              Demo controls
            </div>
            <div className="mt-1.5 grid grid-cols-2 gap-1.5">
              {demoActions.map((a) => (
                <button
                  key={a.key}
                  type="button"
                  onClick={a.run}
                  className="hud-no-drag rounded-md bg-gray-50 px-2 py-1.5 text-left text-[11px] font-medium text-ink-muted transition hover:bg-gray-100 hover:text-ink"
                >
                  {previewLabelFor(a.key) || a.key}
                </button>
              ))}
            </div>
          </HudCard>
        </div>
      )}
    </HudShell>
  );
}
