import { useEffect, useState } from "react";
import {
  settingsBridge,
  NotificationFlags,
  NotificationKey,
} from "../lib/settings-bridge";
import { Switch } from "../components/ui/Switch";

interface SubToggle {
  key: NotificationKey;
  label: string;
  hint?: string;
}

interface Section {
  title: string;
  toggles: readonly SubToggle[];
}

// Grouped sub-toggles. Each section maps to a piece of agent behaviour
// — capture lifecycle toasts, long-meeting prompts, the hotkey
// confirmation, and the daily-drill nudge. The master switch above
// disables every section at once.
const SECTIONS: readonly Section[] = [
  {
    title: "Capture lifecycle",
    toggles: [
      {
        key: "welcome",
        label: "Show the welcome message on first launch",
      },
      {
        key: "post_arm",
        label: "Show “Sayzo is capturing” reminders when a capture starts",
      },
      {
        key: "capture_saved",
        label: "Show “Conversation saved” when a capture finishes",
      },
      {
        key: "session_wrapped",
        label: "Tell me when a capture wraps up automatically",
        hint: "After you tap “Keep going” and the meeting app eventually goes quiet, Sayzo silently saves what you had. This toast confirms it.",
      },
    ],
  },
  {
    title: "Long meetings",
    toggles: [
      {
        key: "checkin",
        label: "Ask if I’m still in long meetings",
        hint: "Pops a “Still in the meeting?” card after 1 hour, then every half hour. Turn off if you prefer captures to run uninterrupted — you can always stop with the hotkey.",
      },
      {
        key: "meeting_ended_watcher",
        label: "Ask when my meeting app stops using the mic",
        hint: "Whitelist-armed sessions only. Off means Sayzo won’t auto-suggest wrap-up; you’ll need to disarm manually when the meeting’s done.",
      },
    ],
  },
  {
    title: "Hotkey",
    toggles: [
      {
        key: "confirm_hotkey_stop",
        label: "Confirm before stopping from the hotkey",
        hint: "Off means a single hotkey press while armed stops the capture instantly — no safety net for accidental presses.",
      },
    ],
  },
  {
    title: "Coaching",
    toggles: [
      {
        key: "daily_drill",
        label: "Send me a daily 60-second speaking drill",
      },
    ],
  },
];

// Used only for the error-recovery default state — every flag false
// so a failed load doesn't show stale "on" toggles. The agent re-syncs
// on next open.
const ALL_FALSE: NotificationFlags = {
  master: false,
  welcome: false,
  post_arm: false,
  capture_saved: false,
  session_wrapped: false,
  checkin: false,
  meeting_ended_watcher: false,
  confirm_hotkey_stop: false,
  daily_drill: false,
};

export function NotificationsPane() {
  const [flags, setFlags] = useState<NotificationFlags | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const f = await settingsBridge.getNotifications();
        if (!cancelled) setFlags(f);
      } catch {
        if (!cancelled) setFlags(ALL_FALSE);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleToggle(key: NotificationKey, value: boolean) {
    setFlags((cur) => (cur ? { ...cur, [key]: value } : cur));
    try {
      const result = await settingsBridge.setNotification(key, value);
      if (!result.saved) {
        setFlags((cur) => (cur ? { ...cur, [key]: !value } : cur));
      }
    } catch {
      setFlags((cur) => (cur ? { ...cur, [key]: !value } : cur));
    }
  }

  if (flags == null) {
    return (
      <div className="text-sm text-ink-muted">Loading notifications…</div>
    );
  }

  const disabled = !flags.master;

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Notifications
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        Choose which Sayzo toasts show up on your desktop.
      </p>

      <ToggleRow
        label="Show Sayzo notifications"
        checked={flags.master}
        onChange={(v) => void handleToggle("master", v)}
      />

      <div className="mt-4 space-y-5">
        {SECTIONS.map((section) => (
          <section key={section.title}>
            <h2
              className={
                "text-xs font-semibold uppercase tracking-wide " +
                (disabled ? "text-ink-muted/70" : "text-ink-muted")
              }
            >
              {section.title}
            </h2>
            <div className="mt-1">
              {section.toggles.map((t) => (
                <ToggleRow
                  key={t.key}
                  label={t.label}
                  hint={t.hint}
                  checked={flags[t.key]}
                  disabled={disabled}
                  onChange={(v) => void handleToggle(t.key, v)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>

      <p className="mt-6 max-w-md text-xs leading-relaxed text-ink-muted">
        The “Was that the end of your meeting?” prompt always shows — it
        gives you a chance to keep going before Sayzo wraps a quiet
        session.
      </p>
    </div>
  );
}

interface ToggleRowProps {
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}

function ToggleRow({ label, hint, checked, disabled, onChange }: ToggleRowProps) {
  return (
    <label
      className={
        "flex items-start justify-between gap-4 rounded-md py-2 " +
        (disabled ? "opacity-50" : "cursor-pointer")
      }
    >
      <div className="flex-1">
        <div className="text-sm text-ink">{label}</div>
        {hint && (
          <div className="mt-0.5 text-xs leading-snug text-ink-muted">
            {hint}
          </div>
        )}
      </div>
      <Switch
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        ariaLabel={label}
      />
    </label>
  );
}
