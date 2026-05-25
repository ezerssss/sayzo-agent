// Typed wrapper around the Python `Bridge` exposed via `window.pywebview.api`.
// All methods are async and round-trip through pywebview's IPC.

export type AccountState =
  | "unknown"
  | "ok"
  | "onboarding_required"
  | "suspended"
  | "deleted";

export type FetchStatus =
  | "ok"
  | "onboarding_required"
  | "suspended"
  | "deleted"
  | "auth_required"
  | "transient_error"
  | "unknown_error";

export type SetupStatus = {
  has_token: boolean;
  has_mic_permission: boolean | null;
  has_permissions_onboarded: boolean;
  // Last observed result of GET /api/me (cached). "unknown" = no cache yet
  // and is treated as pass at the detect layer; everything other than
  // "ok" / "unknown" routes the GUI to the FinishSignup screen.
  account_state: AccountState;
  is_complete: boolean;
  // One-shot resume hint set by Bridge.restart_app() before it hard-exits
  // (currently only "accessibility" in production; "indicator" is set
  // exclusively by the dev mock bridge for the `#preview=indicator`
  // preview route — see `lib/mock-bridge-init.ts`). App.tsx's
  // initialScreen() reads this on the first get_status() call after a
  // Restart-Sayzo round-trip and jumps straight back to the named screen
  // instead of dropping the user to the default sequence[2] (Microphone).
  // Cleared by the backend on read.
  resume_at: "accessibility" | "indicator" | null;
};

export type AccountStatusPayload = {
  status: SetupStatus;
  fetch_status: FetchStatus;
  onboarding_url: string | null;
  error: string | null;
};

export type OpenOnboardingResult = {
  opened: boolean;
  url: string | null;
};

export type ConfigSnapshot = {
  platform: string;
  auth_url: string;
};

export type PermissionResult = {
  granted: boolean | null;
};

// Permission result that also carries the stale-TCC heuristic flag.
// stale_tcc_likely is set by the Python helper when it fingerprints a
// previous-install TCC entry silently denying the request without ever
// presenting UI. Affects mic, audio capture, and notifications on macOS
// (all three go through TCC and key entries by signing identity, so all
// three break when the signing identity changes between installs — as
// it did at v2.6.0 when we added Developer-ID signing). The on-screen
// Settings toggle being ON makes the generic "blocked" copy actively
// misleading in that case, so the screens swap in targeted recovery
// steps when this flag is true.
export type TccPermissionResult = {
  granted: boolean | null;
  stale_tcc_likely: boolean;
};

/** @deprecated Use TccPermissionResult — same shape, generic name. */
export type MicPermissionResult = TccPermissionResult;

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

export type AccessibilityOpenResult = {
  opened: boolean;
};

export type AccessibilityTrustedResult = {
  trusted: boolean;
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

    // Per-permission prompts (one screen each). v2.10+: the Notifications
    // permission flow is gone (custom HUD owns the surface), so only
    // the mic + audio-capture prompts remain.
    prompt_mic_permission(): Promise<TccPermissionResult>;
    prompt_audio_capture_permission(): Promise<TccPermissionResult>;

    // Settings deep-links.
    open_mic_settings(): Promise<null>;
    open_audio_capture_settings(): Promise<null>;
    open_accessibility_settings(): Promise<AccessibilityOpenResult>;

    // macOS-only stale-TCC recovery: clears the orphan entry via
    // `tccutil reset` and relaunches Sayzo. Hard-exits the current
    // setup window — no return value is meaningful.
    reset_mic_permission_and_restart(): Promise<null>;
    reset_audio_capture_permission_and_restart(): Promise<null>;

    // Stuck-user escalation: pull a copy-pasteable diagnostic dump
    // (Info.plist key presence, codesign output, recent log lines)
    // and surface the agent.log folder. Used by the recovery screen's
    // "If Reset & Restart didn't help…" subsection.
    get_tcc_diagnostic_text(): Promise<{ text: string }>;
    copy_tcc_diagnostic_to_clipboard(): Promise<{ copied: boolean }>;
    open_log_folder(): Promise<{ opened: boolean }>;

    // Accessibility verification (polled by setup window after deep-link).
    check_accessibility_trusted(): Promise<AccessibilityTrustedResult>;

    // Hotkey (persisted to user_settings.json).
    get_hotkey(): Promise<HotkeyState>;
    validate_hotkey(binding: string): Promise<HotkeyValidation>;
    save_hotkey(binding: string): Promise<HotkeySaveResult>;

    // Recording indicator (HUD pill visibility). Picked during onboarding,
    // mirrored in Settings → Recording. `visible: true` means the floating
    // capture pill appears on arm; `false` suppresses it (tray icon stays).
    get_recording_indicator(): Promise<{ visible: boolean }>;
    set_recording_indicator(
      visible: boolean,
    ): Promise<{ saved: boolean; error?: string }>;

    mark_permissions_onboarded(): Promise<null>;
    finish(): Promise<null>;
    quit_app(): Promise<null>;
    restart_app(): Promise<null>;

    // Web-onboarding gate.
    recheck_account_status(): Promise<AccountStatusPayload>;
    open_onboarding_url(): Promise<OpenOnboardingResult>;
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

  // Permissions.
  async promptMicPermission() {
    await whenReady();
    return window.pywebview.api.prompt_mic_permission();
  },
  async promptAudioCapturePermission() {
    await whenReady();
    return window.pywebview.api.prompt_audio_capture_permission();
  },
  // promptNotificationPermission / checkNotificationPermission /
  // sendTestNotification removed in v2.10 — see `project_custom_hud_shipped`.
  async openMicSettings() {
    await whenReady();
    return window.pywebview.api.open_mic_settings();
  },
  async openAudioCaptureSettings() {
    await whenReady();
    return window.pywebview.api.open_audio_capture_settings();
  },
  async resetMicPermissionAndRestart() {
    await whenReady();
    return window.pywebview.api.reset_mic_permission_and_restart();
  },
  async resetAudioCapturePermissionAndRestart() {
    await whenReady();
    return window.pywebview.api.reset_audio_capture_permission_and_restart();
  },
  async getTccDiagnosticText() {
    await whenReady();
    return window.pywebview.api.get_tcc_diagnostic_text();
  },
  async copyTccDiagnosticToClipboard() {
    await whenReady();
    return window.pywebview.api.copy_tcc_diagnostic_to_clipboard();
  },
  async openLogFolder() {
    await whenReady();
    return window.pywebview.api.open_log_folder();
  },
  async openAccessibilitySettings() {
    await whenReady();
    return window.pywebview.api.open_accessibility_settings();
  },
  async checkAccessibilityTrusted() {
    await whenReady();
    return window.pywebview.api.check_accessibility_trusted();
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

  // Recording indicator.
  async getRecordingIndicator() {
    await whenReady();
    return window.pywebview.api.get_recording_indicator();
  },
  async setRecordingIndicator(visible: boolean) {
    await whenReady();
    return window.pywebview.api.set_recording_indicator(visible);
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
  async restartApp() {
    await whenReady();
    return window.pywebview.api.restart_app();
  },

  // Web-onboarding gate.
  async recheckAccountStatus() {
    await whenReady();
    return window.pywebview.api.recheck_account_status();
  },
  async openOnboardingUrl() {
    await whenReady();
    return window.pywebview.api.open_onboarding_url();
  },
};
