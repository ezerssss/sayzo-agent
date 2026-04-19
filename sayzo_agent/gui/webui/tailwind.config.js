/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Mirrors the Sayzo marketing site palette: white background,
        // near-black body text, teal accent on CTAs.
        accent: {
          DEFAULT: "#0d9488", // teal-600
          hover: "#0f766e",   // teal-700
          ring: "#5eead4",    // teal-300 — focus ring
        },
        ink: {
          DEFAULT: "#1a1a1a",
          muted: "#6b7280",   // slate-500
          border: "#e5e7eb",  // slate-200
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Inter",
          "system-ui",
          "sans-serif",
        ],
      },
    },
  },
  plugins: [],
};
