// Typed wrapper around the Python `Bridge` exposed via `window.pywebview.api`
// for the Settings window. Mirrors `lib/bridge.ts` (the setup wizard wrapper);
// kept separate so the Settings surface can evolve independently and so each
// app shell only types-up the methods it actually uses.

import { whenReady } from "./bridge";

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

  async finish() {
    await whenReady();
    return window.pywebview.api.finish();
  },
};
