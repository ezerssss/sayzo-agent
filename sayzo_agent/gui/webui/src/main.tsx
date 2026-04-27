// Side-effect import: when `VITE_USE_MOCK_BRIDGE=1`, installs a stub
// `window.pywebview.api` and forces the Settings/Captures route. Tree-
// shaken to nothing in production builds where the env var is unset.
import "./lib/mock-bridge-init";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { SettingsApp } from "./settings/SettingsApp";

// The same bundle is loaded in two contexts: the first-run setup wizard
// (default) and the Settings window (spawned with #route=settings by
// `gui/settings/window.py`). Pick the root component at mount time so the
// Vite bundle, PyInstaller payload, and pywebview asset path stay one each.
//
// Routing parameters live in the URL hash fragment, not the query string —
// pywebview's file:// loader on WebView2 mangles `?` query strings into the
// file path ("file not found: index.html?route=settings"). Hash fragments
// are preserved verbatim and parsed identically client-side.
function readRouteParams(): URLSearchParams {
  const hash = window.location.hash.replace(/^#/, "");
  return new URLSearchParams(hash);
}

function pickRoot(): JSX.Element {
  const params = readRouteParams();
  if (params.get("route") === "settings") {
    return <SettingsApp />;
  }
  return <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>{pickRoot()}</StrictMode>,
);
