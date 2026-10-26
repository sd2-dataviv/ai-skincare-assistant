import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

// API_URL lets a second copy (an end-to-end run, say) use another API without touching the usual one.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.API_URL ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
