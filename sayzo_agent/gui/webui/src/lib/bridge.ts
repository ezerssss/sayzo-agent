// Typed wrapper around the Python `Bridge` exposed via `window.pywebview.api`.
// All methods are async and round-trip through pywebview's IPC.

export type SetupStatus = {
  has_token: boolean;
  has_model: boolean;
  has_mic_permission: boolean | null;
  has_permissions_onboarded: boolean;
  is_complete: boolean;
};

export type ConfigSnapshot = {
  platform: string;
  model_filename: string;
  model_repo: string;
  auth_url: string;
};

export type PermissionResult = {
  granted: boolean | null;
};

export type HotkeyState = {
  binding: string;
  display: string;
};

export type HotkeyValidation = {
  error: string | null;
};

export type HotkeySaveResult = {
  error: string | null;
  display?: string;
};

export type AutomationPromptResult = {
  prompted: string[]; // short labels of browsers we hit
};

export type AccessibilityOpenResult = {
  opened: boolean;
};

// Named interface so other modules (e.g. lib/settings-bridge.ts) can merge
// extra Settings-only methods via TypeScript declaration merging — both
// surfaces live on the same `window.pywebview.api` object at runtime.
declare global {
  interface SayzoPywebviewApi {
    get_status(): Promise<SetupStatus>;
    get_config_snapshot(): Promise<ConfigSnapshot>;
    start_login(): Promise<{ started: boolean }>;
    cancel_login(): Promise<{ cancelled: boolean }>;
    start_model_download(): Promise<{ started: boolean }>;

    // Per-permission prompts (one screen each).
    prompt_mic_permission(): Promise<PermissionResult>;
    prompt_audio_capture_permission(): Promise<PermissionResult>;
    prompt_notification_permission(): Promise<PermissionResult>;
    prompt_automation_permission(): Promise<AutomationPromptResult>;

    // Settings deep-links.
    open_mic_settings(): Promise<null>;
    open_audio_capture_settings(): Promise<null>;
    open_notification_settings(): Promise<null>;
    open_accessibility_settings(): Promise<AccessibilityOpenResult>;

    // Hotkey (persisted to user_settings.json).
    get_hotkey(): Promise<HotkeyState>;
    validate_hotkey(binding: string): Promise<HotkeyValidation>;
    save_hotkey(binding: string): Promise<HotkeySaveResult>;

    mark_permissions_onboarded(): Promise<null>;
    finish(): Promise<null>;
    quit_app(): Promise<null>;
  }

  interface Window {
    pywebview: { api: SayzoPywebviewApi };
  }
}

// Wait until the pywebview JS bridge is fully populated. On macOS's cocoa
// backend, `window.pywebview.api` can exist as an empty object before the
// individual Python methods are bound — so we poll for *some* function being
// present rather than trusting the object's mere existence. The check is
// bridge-agnostic on purpose: the same bundle hosts both the setup wizard
// (Python `Bridge` in gui/setup/bridge.py) and the Settings window (Python
// `Bridge` in gui/settings/bridge.py), and the two expose different methods
// — there is no single method name common to both. We also listen for the
// `pywebviewready` event as a secondary trigger, and bail with a hard error
// after 10s so a truly broken bridge surfaces instead of hanging the UI.
export function whenReady(): Promise<void> {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 10_000;

    const isReady = () => {
      const api = window.pywebview?.api as unknown as
        | Record<string, unknown>
        | undefined;
      if (api == null) return false;
      for (const key in api) {
        if (typeof api[key] === "function") return true;
      }
      return false;
    };

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
  async cancelLogin() {
    await whenReady();
    return window.pywebview.api.cancel_login();
  },
  async startModelDownload() {
    await whenReady();
    return window.pywebview.api.start_model_download();
  },

  // Permissions.
  async promptMicPermission() {
    await whenReady();
    return window.pywebview.api.prompt_mic_permission();
  },
  async promptAudioCapturePermission() {
    await whenReady();
    return window.pywebview.api.prompt_audio_capture_permission();
  },
  async promptNotificationPermission() {
    await whenReady();
    return window.pywebview.api.prompt_notification_permission();
  },
  async promptAutomationPermission() {
    await whenReady();
    return window.pywebview.api.prompt_automation_permission();
  },
  async openMicSettings() {
    await whenReady();
    return window.pywebview.api.open_mic_settings();
  },
  async openAudioCaptureSettings() {
    await whenReady();
    return window.pywebview.api.open_audio_capture_settings();
  },
  async openNotificationSettings() {
    await whenReady();
    return window.pywebview.api.open_notification_settings();
  },
  async openAccessibilitySettings() {
    await whenReady();
    return window.pywebview.api.open_accessibility_settings();
  },

  // Hotkey.
  async getHotkey() {
    await whenReady();
    return window.pywebview.api.get_hotkey();
  },
  async validateHotkey(binding: string) {
    await whenReady();
    return window.pywebview.api.validate_hotkey(binding);
  },
  async saveHotkey(binding: string) {
    await whenReady();
    return window.pywebview.api.save_hotkey(binding);
  },

  async markPermissionsOnboarded() {
    await whenReady();
    return window.pywebview.api.mark_permissions_onboarded();
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
