import { useEffect, useState } from "react";
import { bridge, SetupStatus, ConfigSnapshot } from "./lib/bridge";
import { subscribe, SayzoEvent } from "./lib/events";
import { Welcome } from "./screens/Welcome";
import { Microphone } from "./screens/Microphone";
import { AudioCapture } from "./screens/AudioCapture";
import { Accessibility } from "./screens/Accessibility";
import { Notifications } from "./screens/Notifications";
import { Shortcut } from "./screens/Shortcut";
import { Done } from "./screens/Done";
import { Alert } from "./components/ui/Alert";

// Linear per-platform screen sequence. The first screen (welcome) is
// skippable if the user is already signed in. Everything after is a
// straight walk to the Done screen.
//
// macOS (7): welcome → microphone → audio-capture → accessibility →
//            notifications → shortcut → done
//   (Accessibility doubles as the gate for the global hotkey AND the
//   AX-based web meeting detector — there's no separate Automation
//   screen, since reading browser tab URLs would force the scary
//   "Sayzo wants to control your browser" TCC dialog.)
// Windows (4): welcome → notifications → shortcut → done
type Screen =
  | "loading"
  | "welcome"
  | "microphone"
  | "audio-capture"
  | "accessibility"
  | "notifications"
  | "shortcut"
  | "done";

function sequenceFor(platform: string): Screen[] {
  if (platform === "darwin") {
    return [
      "welcome",
      "microphone",
      "audio-capture",
      "accessibility",
      "notifications",
      "shortcut",
      "done",
    ];
  }
  return ["welcome", "notifications", "shortcut", "done"];
}

function initialScreen(
  status: SetupStatus,
  sequence: Screen[],
): Screen {
  if (!status.has_token) return "welcome";
  // One-shot resume after Restart-Sayzo from the Accessibility screen.
  // The backend writes a marker before exit and clears it on this read,
  // so we only honor it when the target screen is actually in this
  // platform's sequence (defensive — if a future build changes the
  // sequence, a stale marker shouldn't crash us).
  if (
    status.resume_at === "accessibility" &&
    (sequence as string[]).includes("accessibility")
  ) {
    return "accessibility";
  }
  // Already-complete flow won't reach here (detect_setup + .setup-seen gates
  // the window entirely), but guard anyway: jump to the first post-welcome
  // screen so the user can still walk through permissions.
  return sequence[1] ?? "done";
}

function stepLabel(screen: Screen, sequence: Screen[]): string | undefined {
  if (screen === "loading" || screen === "done") return undefined;
  const idx = sequence.indexOf(screen);
  if (idx < 0) return undefined;
  // "01" / "02" / ...
  return String(idx + 1).padStart(2, "0");
}

export function App() {
  const [screen, setScreen] = useState<Screen>("loading");
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [config, setConfig] = useState<ConfigSnapshot | null>(null);
  // Initialised empty; populated by the same Promise.all that fetches
  // status + config below. The loading guard at the bottom of this
  // component blocks rendering until status/config are set, and React
  // batches the three setStates so hotkeyDisplay arrives at the same
  // commit — the empty default never reaches a child screen.
  const [hotkeyDisplay, setHotkeyDisplay] = useState("");
  const [globalError, setGlobalError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, c, h] = await Promise.all([
          bridge.getStatus(),
          bridge.getConfigSnapshot(),
          bridge.getHotkey(),
        ]);
        if (cancelled) return;
        setStatus(s);
        setConfig(c);
        setHotkeyDisplay(h.display);
        setScreen(initialScreen(s, sequenceFor(c.platform)));
      } catch (e) {
        if (!cancelled) setGlobalError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return subscribe(async (evt: SayzoEvent) => {
      if (evt.type === "login_done" && config) {
        const s = await bridge.getStatus();
        setStatus(s);
        // After login, advance to the first post-welcome screen in this
        // platform's sequence.
        const seq = sequenceFor(config.platform);
        const postWelcome = seq[1] ?? "done";
        setScreen(postWelcome);
      }
    });
  }, [config]);

  function handleCancel() {
    void bridge.quitApp();
  }

  function advance() {
    if (!config) return;
    const seq = sequenceFor(config.platform);
    const idx = seq.indexOf(screen);
    if (idx < 0 || idx >= seq.length - 1) {
      setScreen("done");
      return;
    }
    setScreen(seq[idx + 1]);
  }

  if (globalError) {
    return (
      <div className="p-10">
        <Alert>
          <div>
            <strong>Setup error.</strong> {globalError}
          </div>
        </Alert>
      </div>
    );
  }

  if (screen === "loading" || !status || !config) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-muted">
        Loading…
      </div>
    );
  }

  const sequence = sequenceFor(config.platform);
  const step = stepLabel(screen, sequence);

  switch (screen) {
    case "welcome":
      return (
        <Welcome
          onSignedIn={() => {
            // App-level listener advances on login_done; this is a
            // no-op safety hook in case events get lost.
            void bridge.getStatus().then((s) => {
              setStatus(s);
              const seq = sequenceFor(config.platform);
              const postWelcome = seq[1] ?? "done";
              setScreen(postWelcome);
            });
          }}
          onCancel={handleCancel}
        />
      );
    case "microphone":
      return (
        <Microphone
          step={step!}
          onNext={advance}
          onCancel={handleCancel}
        />
      );
    case "audio-capture":
      return (
        <AudioCapture
          step={step!}
          onNext={advance}
          onCancel={handleCancel}
        />
      );
    case "accessibility":
      return (
        <Accessibility
          step={step!}
          onNext={advance}
          onCancel={handleCancel}
        />
      );
    case "notifications":
      return (
        <Notifications
          step={step!}
          platform={config.platform}
          onNext={advance}
          onCancel={handleCancel}
        />
      );
    case "shortcut":
      return (
        <Shortcut
          step={step!}
          onNext={(binding) => {
            // Refresh the hotkey display so the Done screen's copy
            // reflects the user's actual pick.
            void bridge.getHotkey().then((h) => setHotkeyDisplay(h.display));
            // Fallback in case getHotkey races — use the just-saved binding.
            setHotkeyDisplay(
              binding
                .split("+")
                .map((p) => (p.length === 1 ? p.toUpperCase() : titleCase(p)))
                .join("+"),
            );
            setScreen("done");
          }}
          onCancel={handleCancel}
        />
      );
    case "done":
      return <Done hotkeyDisplay={hotkeyDisplay} />;
  }
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
