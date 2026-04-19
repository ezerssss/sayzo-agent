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

// pywebview fires a `pywebviewready` event on window once the JS API is
// usable. Resolves immediately if it already fired.
export function whenReady(): Promise<void> {
  return new Promise((resolve) => {
    if (window.pywebview?.api) {
      resolve();
      return;
    }
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
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
