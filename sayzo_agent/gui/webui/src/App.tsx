import { useEffect, useState } from "react";
import { bridge, SetupStatus, ConfigSnapshot } from "./lib/bridge";
import { subscribe, SayzoEvent } from "./lib/events";
import { Welcome } from "./screens/Welcome";
import { Download } from "./screens/Download";
import { MicPermission } from "./screens/MicPermission";
import { Permissions } from "./screens/Permissions";
import { NotificationsWin } from "./screens/NotificationsWin";
import { Done } from "./screens/Done";
import { Alert } from "./components/ui/Alert";

type Screen =
  | "loading"
  | "welcome"
  | "download"
  | "mic"
  | "permissions"
  | "notifications-win"
  | "done";

// Pick the next screen based on the latest detection. Order matches the
// natural setup sequence: log in → fetch model → grant permissions → done.
// On macOS the Permissions screen is the primary permission step; the
// legacy "mic" recovery screen only fires if the user has already been
// onboarded but audio-tap explicitly reports denial (via recheck).
function nextScreen(status: SetupStatus, platform: string): Screen {
  if (!status.has_token) return "welcome";
  if (!status.has_model) return "download";
  if (platform === "darwin") {
    if (!status.has_permissions_onboarded) return "permissions";
    if (status.has_mic_permission === false) return "mic";
  } else if (platform === "win32") {
    return "notifications-win";
  }
  return "done";
}

export function App() {
  const [screen, setScreen] = useState<Screen>("loading");
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [config, setConfig] = useState<ConfigSnapshot | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);

  // Initial status fetch + ongoing event subscription.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, c] = await Promise.all([
          bridge.getStatus(),
          bridge.getConfigSnapshot(),
        ]);
        if (cancelled) return;
        setStatus(s);
        setConfig(c);
        setScreen(nextScreen(s, c.platform));
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
      if (evt.type === "login_done") {
        // Refresh status and advance.
        const s = await bridge.getStatus();
        setStatus(s);
        if (config) setScreen(nextScreen(s, config.platform));
      } else if (evt.type === "login_error") {
        setGlobalError(evt.message);
      } else if (evt.type === "download_done") {
        const s = await bridge.getStatus();
        setStatus(s);
        if (config) setScreen(nextScreen(s, config.platform));
      }
    });
  }, [config]);

  function handleCancel() {
    void bridge.quitApp();
  }

  async function advanceAfterPermissionsScreen() {
    const s = await bridge.getStatus();
    setStatus(s);
    if (config) {
      // After onboarding, skip the permissions/mic screens and go to done.
      // We call nextScreen but it will now return "done" because the
      // marker is written.
      setScreen(nextScreen(s, config.platform));
    }
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

  switch (screen) {
    case "welcome":
      return (
        <Welcome
          onSignedIn={() => {
            // App-level event listener (above) advances on login_done; this
            // is a no-op safety hook in case events get lost.
            void bridge.getStatus().then((s) => {
              setStatus(s);
              setScreen(nextScreen(s, config.platform));
            });
          }}
          onCancel={handleCancel}
        />
      );
    case "download":
      return (
        <Download
          onDone={() => {
            void bridge.getStatus().then((s) => {
              setStatus(s);
              setScreen(nextScreen(s, config.platform));
            });
          }}
          onCancel={handleCancel}
        />
      );
    case "permissions":
      return (
        <Permissions
          onDone={() => void advanceAfterPermissionsScreen()}
          onCancel={handleCancel}
        />
      );
    case "notifications-win":
      return (
        <NotificationsWin
          onDone={() => setScreen("done")}
          onCancel={handleCancel}
        />
      );
    case "mic":
      return (
        <MicPermission
          onGranted={() => setScreen("done")}
          onCancel={handleCancel}
        />
      );
    case "done":
      return <Done />;
  }
}
