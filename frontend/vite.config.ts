import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const devProxyTarget =
  process.env.VITE_DEV_PROXY_TARGET ?? "http://localhost:8001";
const appBasePath = normalizeBasePath(process.env.VITE_APP_BASE_PATH ?? "/");
const appBasePrefix = appBasePath === "/" ? "" : appBasePath.slice(0, -1);

function normalizeBasePath(basePath: string): string {
  if (!basePath || basePath === "/") {
    return "/";
  }
  return basePath.endsWith("/") ? basePath : `${basePath}/`;
}

function rewriteBasePrefix(path: string): string {
  if (!appBasePrefix || !path.startsWith(appBasePrefix)) {
    return path;
  }
  return path.slice(appBasePrefix.length) || "/";
}

export default defineConfig({
  base: appBasePath,
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": devProxyTarget,
      "/health": devProxyTarget,
      ...(appBasePrefix
        ? {
            [`${appBasePrefix}/api`]: {
              target: devProxyTarget,
              changeOrigin: true,
              rewrite: rewriteBasePrefix,
            },
            [`${appBasePrefix}/health`]: {
              target: devProxyTarget,
              changeOrigin: true,
              rewrite: rewriteBasePrefix,
            },
          }
        : {}),
    },
  },
});
