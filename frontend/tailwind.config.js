/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        tg: {
          bg: "#0f0f0f",
          panel: "#181818",
          panelAlt: "#1c1c1c",
          elevated: "#212121",
          hover: "#2a2a2a",
          border: "#2b2b2b",
          borderSoft: "#242424",
          accent: "#8774e1",
          accentHover: "#9a89e8",
          accentDim: "#5b4fa0",
          accentIncoming: "#2b5278",
          text: "#f4f4f4",
          textMuted: "#a0a0a0",
          textFaint: "#6f6f6f",
          online: "#4dcd5e",
          pause: "#f5a524",
          offline: "#e5484d",
          danger: "#e5484d",
          warning: "#f5a524",
          success: "#4dcd5e",
          info: "#4f8ef7",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "Roboto",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "Cascadia Code",
          "Consolas",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "0.875rem" }],
      },
      boxShadow: {
        bubble: "0 1px 2px rgba(0, 0, 0, 0.5)",
        pop: "0 10px 30px rgba(0, 0, 0, 0.55)",
        glow: "0 0 0 1px rgba(135, 116, 225, 0.35), 0 0 24px rgba(135, 116, 225, 0.18)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-danger": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
        "flash-highlight": {
          "0%": { backgroundColor: "rgba(135, 116, 225, 0.28)" },
          "100%": { backgroundColor: "transparent" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(10px) scale(0.99)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-in": "fade-in 140ms ease-out",
        "pulse-danger": "pulse-danger 1.4s ease-in-out infinite",
        "flash-highlight": "flash-highlight 1.6s ease-out forwards",
        "slide-up": "slide-up 160ms ease-out",
        shimmer: "shimmer 1.6s infinite",
      },
      transitionTimingFunction: {
        tg: "cubic-bezier(0.2, 0, 0.2, 1)",
      },
    },
  },
  plugins: [],
};
