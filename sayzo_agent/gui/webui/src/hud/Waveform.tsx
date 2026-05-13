import { useEffect, useRef, useState } from "react";

interface Props {
  level?: number;
  bars?: number;
}

function phaseFor(i: number, total: number): number {
  return (i / total) * Math.PI * 2;
}

// Decibel mapping on a pre-normalized input. The agent's
// ``Agent._consume`` divides raw RMS by a slow-decaying per-source
// peak, so the value arriving here is already in [0, 1] where 1.0 ≈
// "current peak". The dB shape on top expands the upper range so quiet
// modulations (a brief mid-sentence pause, soft consonants) still
// occupy visible bar height instead of snapping to silence; -30 dB is
// tight because normalization already compressed the dynamic range.
// Calibration on normalized input:
//
//   normalized 0.03 (deep silence)    -> -30 dBFS -> 0.00
//   normalized 0.10 (post-speech)     -> -20 dBFS -> 0.33
//   normalized 0.32 (mid-speech)      -> -10 dBFS -> 0.67
//   normalized 0.56 (rising speech)   ->  -5 dBFS -> 0.83
//   normalized 1.00 (current peak)    ->   0 dBFS -> 1.00
const DB_FLOOR = -30;
function perceptualScale(rms: number): number {
  if (rms <= 0) return 0;
  const db = 20 * Math.log10(rms);
  return Math.max(0, Math.min(1, (db - DB_FLOOR) / -DB_FLOOR));
}

export function Waveform({ level, bars = 7 }: Props) {
  const [tick, setTick] = useState(0);
  const synthLevelRef = useRef(0.45);

  // Tick drives bar-phase wobble whether the level is synthetic or real
  // — without it the bars freeze in a static shape and only their height
  // tracks level, which doesn't look like a waveform.
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 90);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (level !== undefined) return;
    synthLevelRef.current = Math.max(
      0.15,
      Math.min(0.95, synthLevelRef.current + (Math.random() - 0.5) * 0.3),
    );
  }, [level, tick]);

  const activeLevel =
    level !== undefined ? perceptualScale(level) : synthLevelRef.current;

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
