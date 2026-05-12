// Side-effect import: when `VITE_USE_MOCK_BRIDGE=1`, installs a stub
// `window.pywebview.api` and forces the Settings/Captures route. Tree-
// shaken to nothing in production builds where the env var is unset.
import "./lib/mock-bridge-init";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { SettingsApp } from "./settings/SettingsApp";
import { HudApp } from "./HudApp";

// The same bundle is loaded in three contexts: the first-run setup wizard
// (default), the Settings window (#route=settings), and the HUD overlay
// (#route=hud). Pick the root component at mount time so the Vite bundle,
// PyInstaller payload, and pywebview asset path stay one each.
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
  const route = params.get("route");
  if (route === "settings") {
    return <SettingsApp />;
  }
  if (route === "hud") {
    return <HudApp />;
  }
  return <App />;
}

// The HUD route renders into a transparent pywebview window — override the
// global white background set by index.css so we don't paint over the
// frameless transparency. Also strip page-level scroll behaviour, since
// HudShell uses `fixed inset-0` and never needs the viewport to scroll.
function applyHudPageStylesIfNeeded(): void {
  const params = readRouteParams();
  if (params.get("route") !== "hud") return;
  const transparent = "transparent";
  document.documentElement.style.background = transparent;
  document.body.style.background = transparent;
  document.body.style.overflow = "hidden";
  const root = document.getElementById("root");
  if (root) {
    root.style.background = transparent;
    root.style.overflow = "hidden";
  }
}

applyHudPageStylesIfNeeded();

createRoot(document.getElementById("root")!).render(
  <StrictMode>{pickRoot()}</StrictMode>,
);
