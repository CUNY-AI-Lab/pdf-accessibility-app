import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const devProxyTarget =
  process.env.VITE_DEV_PROXY_TARGET ?? "http://127.0.0.1:8791";
const appBasePath = normalizeBasePath(process.env.VITE_APP_BASE_PATH ?? "/");
const appBasePrefix = appBasePath === "/" ? "" : appBasePath.slice(0, -1);

function normalizeBasePath(basePath: string): string {
  if (!basePath || basePath === "/") {
    return "/";
  }
  return basePath.endsWith("/") ? basePath : `${basePath}/`;
}

export default defineConfig({
  base: appBasePath,
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": `${devProxyTarget}/pdf-accessibility`,
      "/health": `${devProxyTarget}/pdf-accessibility`,
      ...(appBasePrefix
        ? {
            [`${appBasePrefix}/api`]: {
              target: devProxyTarget,
              changeOrigin: true,
            },
            [`${appBasePrefix}/health`]: {
              target: devProxyTarget,
              changeOrigin: true,
            },
          }
        : {}),
    },
  },
});
