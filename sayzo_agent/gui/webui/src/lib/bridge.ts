// Typed wrapper around the Python `Bridge` exposed via `window.pywebview.api`.
// All methods are async and round-trip through pywebview's IPC.

export type SetupStatus = {
  has_token: boolean;
  has_model: boolean;
  has_mic_permission: boolean | null;
  is_complete: boolean;
};

export type ConfigSnapshot = {
  platform: string;
  model_filename: string;
  model_repo: string;
  auth_url: string;
};

declare global {
  interface Window {
    pywebview: {
      api: {
        get_status(): Promise<SetupStatus>;
        get_config_snapshot(): Promise<ConfigSnapshot>;
        start_login(): Promise<{ started: boolean }>;
        start_model_download(): Promise<{ started: boolean }>;
        open_mac_privacy_settings(): Promise<null>;
        recheck_mac_permission(): Promise<SetupStatus>;
        finish(): Promise<null>;
        quit_app(): Promise<null>;
      };
    };
  }
}

// Wait until the pywebview JS bridge is fully populated. On macOS's cocoa
// backend, `window.pywebview.api` can exist as an empty object before the
// individual Python methods are bound — so we poll for a specific method
// (`get_status`) rather than trusting the object's mere existence. We also
// listen for the `pywebviewready` event as a secondary trigger, and bail
// with a hard error after 10s so a truly broken bridge surfaces instead of
// hanging the UI forever.
export function whenReady(): Promise<void> {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 10_000;

    const isReady = () =>
      typeof window.pywebview?.api?.get_status === "function";

    if (isReady()) {
      resolve();
      return;
    }

    const tick = () => {
      if (isReady()) {
        resolve();
      } else if (Date.now() > deadline) {
        reject(
          new Error(
            "pywebview JS API never became available. " +
              "If you're on macOS, the Cocoa backend may have failed to bind " +
              "the Python bridge — check the agent log.",
          ),
        );
      } else {
        setTimeout(tick, 20);
      }
    };

    window.addEventListener(
      "pywebviewready",
      () => {
        if (isReady()) resolve();
      },
      { once: true },
    );

    setTimeout(tick, 20);
  });
}

export const bridge = {
  async getStatus() {
    await whenReady();
    return window.pywebview.api.get_status();
  },
  async getConfigSnapshot() {
    await whenReady();
    return window.pywebview.api.get_config_snapshot();
  },
  async startLogin() {
    await whenReady();
    return window.pywebview.api.start_login();
  },
  async startModelDownload() {
    await whenReady();
    return window.pywebview.api.start_model_download();
  },
  async openMacPrivacySettings() {
    await whenReady();
    return window.pywebview.api.open_mac_privacy_settings();
  },
  async recheckMacPermission() {
    await whenReady();
    return window.pywebview.api.recheck_mac_permission();
  },
  async finish() {
    await whenReady();
    return window.pywebview.api.finish();
  },
  async quitApp() {
    await whenReady();
    return window.pywebview.api.quit_app();
  },
};
