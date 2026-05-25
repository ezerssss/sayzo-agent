import { useEffect, useState } from "react";
import {
  settingsBridge,
  RecordingSettings,
  RecordingSettingKey,
} from "../lib/settings-bridge";
import { Switch } from "../components/ui/Switch";

export function RecordingPane() {
  const [settings, setSettings] = useState<RecordingSettings | null>(null);
  const [restartHint, setRestartHint] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await settingsBridge.getRecordingSettings();
        if (!cancelled) setSettings(s);
      } catch {
        if (!cancelled)
          setSettings({
            per_app_capture: false,
            aec_enabled: true,
            show_recording_indicator: true,
          });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleToggle(key: RecordingSettingKey, value: boolean) {
    setSettings((cur) => (cur ? { ...cur, [key]: value } : cur));
    try {
      const result = await settingsBridge.setRecordingSetting(key, value);
      if (!result.saved) {
        setSettings((cur) => (cur ? { ...cur, [key]: !value } : cur));
        return;
      }
      if (result.requires_restart) {
        setRestartHint(true);
      }
    } catch {
      setSettings((cur) => (cur ? { ...cur, [key]: !value } : cur));
    }
  }

  if (settings == null) {
    return <div className="text-sm text-ink-muted">Loading recording settings…</div>;
  }

  return (
    <div>
      <h1 className="text-2xl font-semibold tracking-tight text-ink">
        Recording
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-ink-muted">
        How Sayzo picks up audio when a meeting is being captured.
      </p>

      <div className="mt-8 space-y-2">
        <label className="flex cursor-pointer items-start justify-between gap-4 rounded-md py-2">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">
                Show recording indicator
              </span>
            </div>
            <p className="mt-1 max-w-md text-xs leading-relaxed text-ink-muted">
              A small reminder appears in the corner of your screen while
              Sayzo records. Turn off to stay out of the way — Sayzo still
              records, and the tray icon's menu shows it's running. Takes
              effect the next time Sayzo starts recording.
            </p>
          </div>
          <Switch
            checked={settings.show_recording_indicator}
            onChange={(v) =>
              void handleToggle("show_recording_indicator", v)
            }
            ariaLabel="Show recording indicator"
          />
        </label>

        <label className="flex cursor-pointer items-start justify-between gap-4 rounded-md py-2">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">
                Per-app audio capture
              </span>
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-600">
                Beta
              </span>
            </div>
            <p className="mt-1 max-w-md text-xs leading-relaxed text-ink-muted">
              When you're in a meeting, capture only your meeting app's
              audio (Chrome, Zoom, Slack, etc.) instead of everything
              playing on your computer. Some setups can't isolate by
              app — leave this off if your meeting captures come out
              silent.
            </p>
          </div>
          <Switch
            checked={settings.per_app_capture}
            onChange={(v) => void handleToggle("per_app_capture", v)}
            ariaLabel="Per-app audio capture (beta)"
          />
        </label>

        <label className="flex cursor-pointer items-start justify-between gap-4 rounded-md py-2">
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">
                Echo cancellation
              </span>
            </div>
            <p className="mt-1 max-w-md text-xs leading-relaxed text-ink-muted">
              Reduces echoed voices picked up from your speakers.
              Recommended when not using headphones.
            </p>
          </div>
          <Switch
            checked={settings.aec_enabled}
            onChange={(v) => void handleToggle("aec_enabled", v)}
            ariaLabel="Echo cancellation"
          />
        </label>

        {restartHint && (
          <p className="mt-2 text-xs text-amber-600">
            Restart Sayzo for this change to take effect.
          </p>
        )}
      </div>
    </div>
  );
}
