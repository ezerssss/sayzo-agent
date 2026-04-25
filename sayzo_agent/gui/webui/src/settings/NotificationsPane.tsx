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
}

const SUB_TOGGLES: readonly SubToggle[] = [
  { key: "welcome", label: "Show the welcome message on first launch" },
  { key: "post_arm", label: "Show “Sayzo is capturing” reminders after I arm" },
  { key: "capture_saved", label: "Show “Conversation saved” when a capture finishes" },
];

export function NotificationsPane() {
  const [flags, setFlags] = useState<NotificationFlags | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const f = await settingsBridge.getNotifications();
        if (!cancelled) setFlags(f);
      } catch {
        if (!cancelled) {
          setFlags({
            master: false,
            welcome: false,
            post_arm: false,
            capture_saved: false,
          });
        }
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
        // Roll back the optimistic update on persistence failure.
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

      <div className="mt-2 space-y-1 pl-6">
        {SUB_TOGGLES.map((t) => (
          <ToggleRow
            key={t.key}
            label={t.label}
            checked={flags[t.key]}
            disabled={!flags.master}
            onChange={(v) => void handleToggle(t.key, v)}
          />
        ))}
      </div>

      <p className="mt-6 max-w-md text-xs leading-relaxed text-ink-muted">
        Consent prompts and end-of-meeting questions always show — they're
        how you decide what Sayzo captures.
      </p>
    </div>
  );
}

interface ToggleRowProps {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}

function ToggleRow({ label, checked, disabled, onChange }: ToggleRowProps) {
  return (
    <label
      className={
        "flex items-center justify-between gap-4 rounded-md py-2 " +
        (disabled ? "opacity-50" : "cursor-pointer")
      }
    >
      <span className="text-sm text-ink">{label}</span>
      <Switch
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        ariaLabel={label}
      />
    </label>
  );
}
