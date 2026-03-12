import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const devProxyTarget =
  process.env.VITE_DEV_PROXY_TARGET ?? "http://localhost:8001";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": devProxyTarget,
      "/health": devProxyTarget,
    },
  },
});
