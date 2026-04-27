// Typed wrapper around the Python `Bridge` exposed via `window.pywebview.api`
// for the Settings window. Mirrors `lib/bridge.ts` (the setup wizard wrapper);
// kept separate so the Settings surface can evolve independently and so each
// app shell only types-up the methods it actually uses.

import { whenReady } from "./bridge";
import { buildMockCaptures } from "./captures-mock";

// `npm run dev:mock` sets VITE_USE_MOCK_BRIDGE=1 (loaded from .env.mock via
// `vite --mode mock`) so the Captures pane can be designed against realistic
// fake data without a running Python agent. Production builds run with the
// flag unset and hit the real bridge.
const USE_MOCK_CAPTURES = import.meta.env.VITE_USE_MOCK_BRIDGE === "1";

const mockCapturesState: { captures: CaptureSummary[] } | null =
  USE_MOCK_CAPTURES ? { captures: buildMockCaptures() } : null;

// ---- Response shapes ------------------------------------------------------

export type AccountStatus =
  | { state: "signed_out" }
  | {
      state: "signed_in";
      signed_in_since: string | null; // ISO8601 UTC, file mtime
      server: string;
    };

export type AboutInfo = {
  version: string;
  platform: string; // sys.platform: "win32" | "darwin" | "linux"
  platform_human: string; // e.g. "Windows-10-…"
  python_version: string;
  captures_dir: string;
  logs_dir: string;
  data_dir: string;
  web_app_url: string;
  support_url: string;
};

export type Diagnostics = {
  text: string;
};

export type NotificationFlags = {
  master: boolean;
  welcome: boolean;
  post_arm: boolean;
  capture_saved: boolean;
};

export type NotificationKey = keyof NotificationFlags;

export type PermissionRow = {
  key: string;
  label: string;
  description: string;
};

export type PermissionResult = {
  granted: boolean | null;
};

export type PermissionOpenResult = {
  opened: boolean;
};

// ---- Meeting Apps --------------------------------------------------------

export type DetectorKind = "desktop" | "web";

export type DetectorSummary = {
  app_key: string;
  display_name: string;
  kind: DetectorKind;
  detail: string;
  is_browser: boolean;
  process_names: string[];
  bundle_ids: string[];
  url_patterns: string[];
  title_patterns: string[];
  disabled: boolean;
};

export type SeenAppSummary = {
  key: string;
  display_name: string;
  process_name: string | null;
  bundle_id: string | null;
};

export type MicHolderSnapshot = {
  process_name: string;
  pid: number;
  // Pre-computed by the agent so the polling Add-app dialog doesn't need
  // an extra IPC round-trip per row to filter browsers out of the
  // desktop-app picker.
  is_browser: boolean;
};

export type MicStateSnapshot = {
  holders: MicHolderSnapshot[];
  active: boolean;
  running_processes: string[];
};

export type ForegroundSnapshot = {
  process_name?: string | null;
  bundle_id?: string | null;
  window_title?: string | null;
  browser_tab_url?: string | null;
  browser_tab_title?: string | null;
  is_browser?: boolean;
  browser_window_titles?: string[];
  browser_window_urls?: string[];
};

// Result shapes use a nullable `error` plus optional success fields rather
// than a discriminated union: TypeScript's narrowing on `if (result.error)`
// gets fragile when the error branch overlaps with `string`, and the bridge
// already guarantees the success fields are populated whenever `error` is
// null. Callers handle `null` first, then read the optional fields.
export type ParsedMeetingUrl = {
  error: string | null;
  host?: string;
  path?: string;
  display_name?: string;
};

export type BuiltUrlPattern = {
  error: string | null;
  pattern?: string;
};

// New-spec input shape — Python validates via `DetectorSpec.model_validate`,
// so any field on `DetectorSummary` is acceptable here. The required pair is
// `app_key` + `display_name`; everything else has a sensible default in
// pydantic.
export type DetectorSpecInput = {
  app_key: string;
  display_name: string;
  is_browser?: boolean;
  process_names?: string[];
  bundle_ids?: string[];
  url_patterns?: string[];
  title_patterns?: string[];
  disabled?: boolean;
};

// ---- Captures pane ------------------------------------------------------

export type CaptureBucket =
  | "in_progress"
  | "uploaded"
  | "failed"
  | "skipped";

export type CaptureStatusKey =
  | "processing"
  | "pending"
  | "uploading"
  | "uploaded"
  | "failed_transient"
  | "failed_permanent"
  | "credit_blocked"
  | "auth_blocked"
  | "dropped";

export type CaptureSummary = {
  id: string;
  title: string;
  started_at: string; // ISO
  ended_at: string; // ISO
  duration_secs: number;
  status: CaptureStatusKey;
  bucket: CaptureBucket;
  badge_label: string;
  badge_tone: "gray" | "blue" | "green" | "amber" | "red";
  detail: string | null;
  attempts: number;
  next_attempt_at: string | null;
  has_audio: boolean;
  is_processing: boolean;
  dropped_reason: string | null;
};

export type CaptureDeleteResult = {
  deleted: boolean;
  error?: string;
};

export type CaptureRetryResult = {
  retrying: boolean;
  error?: string;
};

export type CaptureOpenResult = {
  opened: boolean;
  error?: string;
};

// ---- Settings-only window.pywebview.api surface --------------------------
// Augments the `SayzoPywebviewApi` interface declared in `lib/bridge.ts`.
// Both surfaces live on the same `window.pywebview.api` object at runtime;
// TypeScript merges the interface declarations into a single type.

declare global {
  interface SayzoPywebviewApi {
    // General.
    get_initial_pane(): Promise<string | null>;
    get_about_info(): Promise<AboutInfo>;
    open_captures_folder(): Promise<null>;
    open_logs_folder(): Promise<null>;
    open_url(url: string): Promise<null>;
    get_diagnostics(): Promise<Diagnostics>;

    // Account. start_login / cancel_login are already on the setup surface;
    // the Python-side bridge instance for Settings re-exposes them with the
    // same shape, so no extra type entries are needed here.
    account_status(): Promise<AccountStatus>;
    sign_out(): Promise<{ signed_out: boolean }>;

    // About.
    check_for_update(): Promise<{ checking: boolean }>;

    // Notifications.
    get_notifications(): Promise<NotificationFlags>;
    set_notification(
      key: NotificationKey,
      value: boolean,
    ): Promise<{ saved: boolean; error?: string }>;

    // Permissions.
    get_permissions(): Promise<PermissionRow[]>;
    request_permission(key: string): Promise<PermissionResult>;
    open_permission_settings(key: string): Promise<PermissionOpenResult>;

    // Meeting Apps.
    list_detectors(): Promise<DetectorSummary[]>;
    toggle_detector(
      app_key: string,
      enabled: boolean,
    ): Promise<{ saved: boolean; error?: string }>;
    remove_detector(
      app_key: string,
    ): Promise<{ removed: boolean; saved?: boolean; error?: string }>;
    add_detector(
      spec: DetectorSpecInput,
    ): Promise<{ added: boolean; saved?: boolean; error?: string }>;
    reset_detectors(): Promise<{ reset: boolean; error?: string }>;
    list_seen_apps(): Promise<SeenAppSummary[]>;
    dismiss_seen_app(app_key: string): Promise<{ dismissed: boolean }>;
    snapshot_mic_state(): Promise<MicStateSnapshot>;
    snapshot_foreground(): Promise<ForegroundSnapshot>;
    parse_meeting_url(url: string): Promise<ParsedMeetingUrl>;
    build_url_pattern(
      host: string,
      path: string,
      strict: boolean,
    ): Promise<BuiltUrlPattern>;
    make_app_key(seed: string): Promise<string>;

    // Captures pane.
    list_captures(): Promise<CaptureSummary[]>;
    delete_capture(capture_id: string): Promise<CaptureDeleteResult>;
    retry_capture_upload(capture_id: string): Promise<CaptureRetryResult>;
    open_capture_folder(capture_id: string): Promise<CaptureOpenResult>;
  }
}

export const settingsBridge = {
  async getInitialPane() {
    await whenReady();
    return window.pywebview.api.get_initial_pane();
  },
  async getAboutInfo() {
    await whenReady();
    return window.pywebview.api.get_about_info();
  },
  async openCapturesFolder() {
    await whenReady();
    return window.pywebview.api.open_captures_folder();
  },
  async openLogsFolder() {
    await whenReady();
    return window.pywebview.api.open_logs_folder();
  },
  async openUrl(url: string) {
    await whenReady();
    return window.pywebview.api.open_url(url);
  },
  async getDiagnostics() {
    await whenReady();
    return window.pywebview.api.get_diagnostics();
  },

  async accountStatus() {
    await whenReady();
    return window.pywebview.api.account_status();
  },
  async startLogin() {
    await whenReady();
    return window.pywebview.api.start_login();
  },
  async cancelLogin() {
    await whenReady();
    return window.pywebview.api.cancel_login();
  },
  async signOut() {
    await whenReady();
    return window.pywebview.api.sign_out();
  },

  async checkForUpdate() {
    await whenReady();
    return window.pywebview.api.check_for_update();
  },

  async getNotifications() {
    await whenReady();
    return window.pywebview.api.get_notifications();
  },
  async setNotification(key: NotificationKey, value: boolean) {
    await whenReady();
    return window.pywebview.api.set_notification(key, value);
  },

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

  async getPermissions() {
    await whenReady();
    return window.pywebview.api.get_permissions();
  },
  async requestPermission(key: string) {
    await whenReady();
    return window.pywebview.api.request_permission(key);
  },
  async openPermissionSettings(key: string) {
    await whenReady();
    return window.pywebview.api.open_permission_settings(key);
  },

  async listDetectors() {
    await whenReady();
    return window.pywebview.api.list_detectors();
  },
  async toggleDetector(appKey: string, enabled: boolean) {
    await whenReady();
    return window.pywebview.api.toggle_detector(appKey, enabled);
  },
  async removeDetector(appKey: string) {
    await whenReady();
    return window.pywebview.api.remove_detector(appKey);
  },
  async addDetector(spec: DetectorSpecInput) {
    await whenReady();
    return window.pywebview.api.add_detector(spec);
  },
  async resetDetectors() {
    await whenReady();
    return window.pywebview.api.reset_detectors();
  },
  async listSeenApps() {
    await whenReady();
    return window.pywebview.api.list_seen_apps();
  },
  async dismissSeenApp(appKey: string) {
    await whenReady();
    return window.pywebview.api.dismiss_seen_app(appKey);
  },
  async snapshotMicState() {
    await whenReady();
    return window.pywebview.api.snapshot_mic_state();
  },
  async snapshotForeground() {
    await whenReady();
    return window.pywebview.api.snapshot_foreground();
  },
  async parseMeetingUrl(url: string) {
    await whenReady();
    return window.pywebview.api.parse_meeting_url(url);
  },
  async buildUrlPattern(host: string, path: string, strict: boolean) {
    await whenReady();
    return window.pywebview.api.build_url_pattern(host, path, strict);
  },
  async makeAppKey(seed: string) {
    await whenReady();
    return window.pywebview.api.make_app_key(seed);
  },

  // ---- Captures ----------------------------------------------------------

  async listCaptures(): Promise<CaptureSummary[]> {
    if (mockCapturesState) {
      return Promise.resolve(
        mockCapturesState.captures.map((c) => ({ ...c })),
      );
    }
    await whenReady();
    return window.pywebview.api.list_captures();
  },

  async deleteCapture(captureId: string): Promise<CaptureDeleteResult> {
    if (mockCapturesState) {
      const before = mockCapturesState.captures.length;
      mockCapturesState.captures = mockCapturesState.captures.filter(
        (c) => c.id !== captureId,
      );
      return Promise.resolve({ deleted: mockCapturesState.captures.length < before });
    }
    await whenReady();
    return window.pywebview.api.delete_capture(captureId);
  },

  async retryCaptureUpload(captureId: string): Promise<CaptureRetryResult> {
    if (mockCapturesState) {
      const c = mockCapturesState.captures.find((x) => x.id === captureId);
      if (!c) return Promise.resolve({ retrying: false });
      c.status = "uploading";
      c.bucket = "in_progress";
      c.badge_label = "Uploading…";
      c.badge_tone = "blue";
      c.detail = null;
      c.attempts = (c.attempts ?? 0) + 1;
      // Simulate a successful upload landing a couple of seconds later so
      // the demo feels alive when the user clicks "Try again now".
      setTimeout(() => {
        if (!mockCapturesState) return;
        const stillThere = mockCapturesState.captures.find((x) => x.id === captureId);
        if (!stillThere) return;
        stillThere.status = "uploaded";
        stillThere.bucket = "uploaded";
        stillThere.badge_label = "Saved to your account";
        stillThere.badge_tone = "green";
        stillThere.detail = null;
      }, 2000);
      return Promise.resolve({ retrying: true });
    }
    await whenReady();
    return window.pywebview.api.retry_capture_upload(captureId);
  },

  async openCaptureFolder(captureId: string): Promise<CaptureOpenResult> {
    if (mockCapturesState) {
      console.info("[mock] open_capture_folder", captureId);
      return Promise.resolve({ opened: true });
    }
    await whenReady();
    return window.pywebview.api.open_capture_folder(captureId);
  },

  async finish() {
    await whenReady();
    return window.pywebview.api.finish();
  },
};
