import { useEffect } from "react";
import { hudBridge } from "./hud-bridge";

/**
 * Emit a `card_painted` event to Python after one rAF following the
 * component's mount or id change.
 *
 * Used by every HUD overlay component (ConsentCard, ActionableToast,
 * InsightCard, InfoToast, StatePill) so the launcher can log delta_ms
 * between the `show_X` command and the browser's first paint of the
 * mounted element. Diagnoses the layered-window paint-stall described
 * in `gui/hud/window.py:319-326` — if delta_ms is reasonable but the
 * user still sees nothing, the failure is in Qt's UpdateLayeredWindow
 * compose path; if the event never fires, the failure is React-side.
 *
 * Passes the id through as `request_id` even though each component
 * names its prop differently (`requestId` / `id` / `paintId`); the
 * naming asymmetry stays at the component boundary so each component's
 * API matches the rest of its props.
 *
 * Accepts `undefined` so dev-preview callers without a launcher-issued
 * id (e.g. StatePill under `npm run dev:hud`) silently skip the emit
 * instead of polluting telemetry with `request_id=undefined`.
 */
export function usePaintedSignal(id: string | undefined): void {
  useEffect(() => {
    if (!id) return;
    const raf = requestAnimationFrame(() => {
      void hudBridge.sendEvent({ event: "card_painted", request_id: id });
    });
    return () => cancelAnimationFrame(raf);
  }, [id]);
}
