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

// HUD-only page setup. The HUD route runs inside a Qt
// `QWebEngineView` whose host widget uses `WA_TranslucentBackground`,
// so the page's transparent regions are genuinely click-through.
// Three things matter:
//
// 1. Backgrounds must be transparent so Qt's alpha compositor sees
//    the page's per-pixel alpha (Chromium paints white by default
//    otherwise).
// 2. Page sizes to content via `fit-content` so the ResizeObserver
//    in `HudApp.tsx` reports the actual content rect — overrides
//    the `height: 100%` set in `index.css` for the Settings / Setup
//    routes.
// 3. Page-level scroll is killed — nothing to scroll, and a stray
//    scrollbar would offset the content from the window edge.
function applyHudPageStylesIfNeeded(): void {
  const params = readRouteParams();
  if (params.get("route") !== "hud") return;
  const transparent = "transparent";
  const fit = "fit-content";
  document.documentElement.style.background = transparent;
  document.documentElement.style.width = fit;
  document.documentElement.style.height = fit;
  document.body.style.background = transparent;
  document.body.style.overflow = "hidden";
  document.body.style.width = fit;
  document.body.style.height = fit;
  const root = document.getElementById("root");
  if (root) {
    root.style.background = transparent;
    root.style.overflow = "hidden";
    root.style.width = fit;
    root.style.height = fit;
  }
}

applyHudPageStylesIfNeeded();

createRoot(document.getElementById("root")!).render(
  <StrictMode>{pickRoot()}</StrictMode>,
);
