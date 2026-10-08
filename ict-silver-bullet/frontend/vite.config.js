import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
const outDir = process.env.ICT_BUILD_DIR || "dist";
const apiPort = process.env.ICT_API_PORT || "7860";
if (!/^\d+$/.test(apiPort) || +apiPort < 1 || +apiPort > 65535)
  throw new Error("Invalid API port");
if (!["dist", "dist.next"].includes(outDir))
  throw new Error("Invalid build directory");
export default defineConfig({
  plugins: [react()],
  build: { outDir, emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
      "/healthz": {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: false,
      },
    },
  },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
});
