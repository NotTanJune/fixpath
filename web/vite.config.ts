import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { brandPlugin } from "./brand-plugin.ts";

export default defineConfig({
  plugins: [react(), brandPlugin()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
    fs: { allow: [".."] },
  },
  build: { chunkSizeWarningLimit: 1000 },
});
