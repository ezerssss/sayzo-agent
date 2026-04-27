import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// pywebview loads dist/index.html via file:// — Vite's default absolute
// asset paths break under file://, so flip to relative.
//
// Brand assets (logo.png) live at installer/assets/ as the canonical
// source — they're shipped by PyInstaller for the taskbar/dock icon.
// The webui keeps a synced copy at src/assets/logo.png so Vite's bundler
// can pick it up without crossing the project root. Kept in sync via
// scripts/sync_brand_assets.py (run as the npm `prebuild` script).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Inline small assets to keep the bundle to a single HTML + JS + CSS triplet —
    // simpler to ship inside PyInstaller and easier to debug from a frozen build.
    assetsInlineLimit: 100_000,
  },
});
