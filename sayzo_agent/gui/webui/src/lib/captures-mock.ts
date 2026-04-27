// Realistic mock captures used by `npm run dev:mock` so the Captures pane
// can be designed, tweaked, and previewed without booting the full Python
// agent. Build once at module import; mutating ops (delete / retry) edit
// the live array so the UI reacts the way it would in production.

import type { CaptureSummary, CaptureBucket, CaptureStatusKey } from "./settings-bridge";

function isoMinutesAgo(min: number): string {
  return new Date(Date.now() - min * 60_000).toISOString();
}

function isoHoursAgo(h: number): string {
  return isoMinutesAgo(h * 60);
}

function isoDaysAgo(d: number): string {
  return isoMinutesAgo(d * 24 * 60);
}

function hexId(seed: number): string {
  // Deterministic 12-hex id for stable test rendering.
  let s = (seed * 2654435761) >>> 0;
  let out = "";
  for (let i = 0; i < 3; i++) {
    out += s.toString(16).padStart(8, "0");
    s = (s * 1103515245 + 12345) >>> 0;
  }
  return out.slice(0, 12);
}

type MockSpec = {
  status: CaptureStatusKey;
  bucket: CaptureBucket;
  badge_label: string;
  badge_tone: CaptureSummary["badge_tone"];
  title: string;
  startedMinutesAgo: number;
  duration_secs: number;
  detail?: string;
  attempts?: number;
  next_attempt_at?: string | null;
  has_audio?: boolean;
  is_processing?: boolean;
  dropped_reason?: string | null;
};

const SPECS: MockSpec[] = [
  {
    status: "processing",
    bucket: "in_progress",
    badge_label: "Sayzo is analyzing this",
    badge_tone: "blue",
    title: "Untitled meeting",
    startedMinutesAgo: 1,
    duration_secs: 92,
    has_audio: false,
    is_processing: true,
  },
  {
    status: "uploading",
    bucket: "in_progress",
    badge_label: "Uploading…",
    badge_tone: "blue",
    title: "Standup with the design team",
    startedMinutesAgo: 8,
    duration_secs: 22 * 60,
  },
  {
    status: "pending",
    bucket: "in_progress",
    badge_label: "Waiting to upload",
    badge_tone: "gray",
    title: "Quick sync with Maya",
    startedMinutesAgo: 25,
    duration_secs: 14 * 60,
  },
  {
    status: "auth_blocked",
    bucket: "in_progress",
    badge_label: "Sign in to keep uploading",
    badge_tone: "amber",
    title: "Customer call — Acme onboarding",
    startedMinutesAgo: 42,
    duration_secs: 35 * 60,
    detail: "Your Sayzo session expired.",
  },
  {
    status: "credit_blocked",
    bucket: "in_progress",
    badge_label: "Paused — Sayzo limit reached",
    badge_tone: "amber",
    title: "Investor pitch dry-run",
    startedMinutesAgo: 80,
    duration_secs: 41 * 60,
    detail: "You've used all your free Sayzo actions.",
  },
  {
    status: "uploaded",
    bucket: "uploaded",
    badge_label: "Saved to your account",
    badge_tone: "green",
    title: "1:1 with Priya",
    startedMinutesAgo: 60 * 4,
    duration_secs: 28 * 60,
  },
  {
    status: "uploaded",
    bucket: "uploaded",
    badge_label: "Saved to your account",
    badge_tone: "green",
    title: "Roadmap planning Q3",
    startedMinutesAgo: 60 * 26,
    duration_secs: 56 * 60,
  },
  {
    status: "uploaded",
    bucket: "uploaded",
    badge_label: "Saved to your account",
    badge_tone: "green",
    title: "Coffee chat — Daniel from Linear",
    startedMinutesAgo: 60 * 50,
    duration_secs: 32 * 60,
  },
  {
    status: "uploaded",
    bucket: "uploaded",
    badge_label: "Saved to your account",
    badge_tone: "green",
    title: "Weekly all-hands",
    startedMinutesAgo: 60 * 24 * 5,
    duration_secs: 47 * 60,
  },
  {
    status: "failed_transient",
    bucket: "failed",
    badge_label: "Will try again soon",
    badge_tone: "amber",
    title: "Demo with Northwind",
    startedMinutesAgo: 60 * 2 + 10,
    duration_secs: 19 * 60,
    detail: "Network connection lost — Sayzo will retry in a few minutes.",
    attempts: 2,
    next_attempt_at: new Date(Date.now() + 6 * 60_000).toISOString(),
  },
  {
    status: "failed_permanent",
    bucket: "failed",
    badge_label: "Couldn't upload",
    badge_tone: "red",
    title: "Brainstorm with Lin",
    startedMinutesAgo: 60 * 30,
    duration_secs: 12 * 60,
    detail: "The server rejected this capture.",
    attempts: 3,
  },
  {
    status: "dropped",
    bucket: "skipped",
    badge_label: "Skipped — not enough conversation",
    badge_tone: "gray",
    title: "Untitled meeting",
    startedMinutesAgo: 17,
    duration_secs: 22,
    detail: "This was very short or mostly silence.",
    has_audio: false,
    dropped_reason: "gate_failed",
  },
  {
    status: "dropped",
    bucket: "skipped",
    badge_label: "Skipped — wasn't English",
    badge_tone: "gray",
    title: "Untitled meeting",
    startedMinutesAgo: 60 * 6,
    duration_secs: 4 * 60,
    detail: "Sayzo only coaches English right now (heard ES).",
    has_audio: false,
    dropped_reason: "non_english",
  },
  {
    status: "dropped",
    bucket: "skipped",
    badge_label: "Skipped — nothing was transcribed",
    badge_tone: "gray",
    title: "Untitled meeting",
    startedMinutesAgo: 60 * 14,
    duration_secs: 88,
    detail: "Sayzo couldn't make out any speech.",
    has_audio: false,
    dropped_reason: "empty_transcript",
  },
  {
    status: "dropped",
    bucket: "skipped",
    badge_label: "Sayzo decided not to keep this",
    badge_tone: "gray",
    title: "Untitled meeting",
    startedMinutesAgo: 60 * 22,
    duration_secs: 6 * 60,
    detail: "It didn't look like a real conversation.",
    has_audio: false,
    dropped_reason: "llm_rejected",
  },
];

export function buildMockCaptures(): CaptureSummary[] {
  return SPECS.map((spec, idx) => {
    const startedAt = isoMinutesAgo(spec.startedMinutesAgo);
    const endedAt = isoMinutesAgo(
      Math.max(0, spec.startedMinutesAgo - spec.duration_secs / 60),
    );
    return {
      id: hexId(idx + 1),
      title: spec.title,
      started_at: startedAt,
      ended_at: endedAt,
      duration_secs: spec.duration_secs,
      status: spec.status,
      bucket: spec.bucket,
      badge_label: spec.badge_label,
      badge_tone: spec.badge_tone,
      detail: spec.detail ?? null,
      attempts: spec.attempts ?? 0,
      next_attempt_at: spec.next_attempt_at ?? null,
      has_audio: spec.has_audio ?? true,
      is_processing: spec.is_processing ?? false,
      dropped_reason: spec.dropped_reason ?? null,
    };
  });
}

// Re-export the helpers so tests / dev tools can simulate transitions.
export { isoMinutesAgo, isoHoursAgo, isoDaysAgo, hexId };
