import { useEffect, useRef, useState } from "react";

interface Props {
  level?: number;
  bars?: number;
}

function phaseFor(i: number, total: number): number {
  return (i / total) * Math.PI * 2;
}

export function Waveform({ level, bars = 7 }: Props) {
  const [tick, setTick] = useState(0);
  const synthLevelRef = useRef(0.45);

  // Tick only drives the synthetic animation. When the agent is
  // streaming real audio (`level !== undefined`), the parent re-renders
  // via setAudioLevel and we don't need an internal interval.
  useEffect(() => {
    if (level !== undefined) return;
    const id = setInterval(() => setTick((t) => t + 1), 90);
    return () => clearInterval(id);
  }, [level]);

  useEffect(() => {
    if (level !== undefined) return;
    synthLevelRef.current = Math.max(
      0.15,
      Math.min(0.95, synthLevelRef.current + (Math.random() - 0.5) * 0.3),
    );
  }, [level, tick]);

  const activeLevel = level !== undefined ? level : synthLevelRef.current;

  const t = tick * 0.18;
  const heights: number[] = [];
  for (let i = 0; i < bars; i++) {
    const phase = phaseFor(i, bars);
    const wobble = 0.55 + 0.45 * Math.abs(Math.sin(t + phase));
    const h = Math.max(0.15, Math.min(1, activeLevel * wobble));
    heights.push(h);
  }

  return (
    <div className="flex h-6 items-center gap-[3px]" aria-hidden>
      {heights.map((h, i) => (
        <div
          key={i}
          className="w-[3px] rounded-full bg-accent"
          style={{
            height: `${Math.round(h * 100)}%`,
            transition: "height 120ms ease-out",
          }}
        />
      ))}
    </div>
  );
}
