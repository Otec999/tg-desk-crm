import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Vite configuration.
 *
 * The SPA talks straight to the FastAPI backend (CORS is configured
 * server-side), so the exact same build works when the frontend is served
 * from any static host later on.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    host: "127.0.0.1",
    open: false,
    watch: {
      /**
       * Editors that save atomically create short-lived `.*.tmpdir` folders next to the
       * file. On Windows the FS watcher can hit EBUSY on them and kill the dev server, so
       * these patterns are excluded from watching entirely.
       */
      ignored: ["**/.vite/**", "**/*.tmpdir/**", "**/*.tmp", "**/dist/**"],
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2020",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          state: ["zustand"],
          icons: ["lucide-react"],
        },
      },
    },
  },
});
