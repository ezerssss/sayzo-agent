import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// pywebview loads dist/index.html via file:// — Vite's default absolute
// asset paths break under file://, so flip to relative.
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
