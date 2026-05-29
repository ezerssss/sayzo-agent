import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Countdown timer with a single-fire latch shared between the timer's
 * own expiry and any external callers (click handlers, dismiss
 * buttons). Returns ``remaining`` for driving a progress-bar UI and
 * ``fireOnce`` for the click handlers to call — both go through the
 * same latch, so neither path can double-fire ``onFire`` and the
 * caller doesn't need to maintain its own `calledRef`.
 *
 * The ``onFire`` callback is read via a ref so the countdown effect
 * can drop it from its deps. Parents typically pass an inline arrow
 * (`onAnswer={(a) => handleCardAnswer(activeCard.request_id, a)}`)
 * that changes reference on every re-render; putting that in deps
 * would clear+restart the setInterval on every parent render and
 * prevent the timer from ever reaching expiry. Pre-v3.11 this was a
 * real production bug for consents fired WHILE armed (long-meeting
 * check-in, meeting-ended, pending_close): audio levels stream at
 * 10-20Hz → HudApp re-renders 10-20×/sec → timer reset every tick →
 * Python's grace-timeout fired with a ``TimeoutError`` traceback
 * instead of the React-side ``card_response`` / ``actionable_response``
 * / ``insight_response`` event.
 *
 * **Stability contract for callers:** pass ``totalSecs``, ``expiredValue``,
 * and ``tickMs`` as referentially-stable values (literals, primitives,
 * or values from a parent's useRef / useMemo). All three are in the
 * countdown effect's deps; a new reference on any of them restarts the
 * interval and re-anchors ``startedAt`` to ``Date.now()``, silently
 * re-introducing the same reset bug this hook was built to fix. The
 * three current call sites (ConsentCard / ActionableToast /
 * InsightCard) all pass literals, so the deps fire exactly once on
 * mount. If you add a caller that wants a dynamic ``tickMs``, route
 * it through a ref instead of putting state in the prop.
 */
export function useCountdownTimer<T extends string>(
  totalSecs: number,
  expiredValue: T,
  onFire: (value: T) => void,
  tickMs: number = 100,
): { remaining: number; fireOnce: (value: T) => void } {
  const [remaining, setRemaining] = useState(totalSecs);
  const calledRef = useRef(false);
  const onFireRef = useRef(onFire);

  useEffect(() => {
    onFireRef.current = onFire;
  }, [onFire]);

  const fireOnce = useCallback((value: T) => {
    if (calledRef.current) return;
    calledRef.current = true;
    onFireRef.current(value);
  }, []);

  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      const left = Math.max(0, totalSecs - elapsed);
      setRemaining(left);
      if (left <= 0 && !calledRef.current) {
        calledRef.current = true;
        clearInterval(id);
        onFireRef.current(expiredValue);
      }
    }, tickMs);
    return () => clearInterval(id);
  }, [totalSecs, expiredValue, tickMs]);

  return { remaining, fireOnce };
}
