/**
 * PDF Accessibility v2 behind Doorway, mounted at BASE_PATH:
 *   /api/*     the React app's API, for a person Doorway signed in
 *   /runner/*  the pipeline host's API (Doorway's public route; own credential)
 *   /health    liveness
 *   anything else: the React app's static files
 */
import { authenticate } from "./identity";
import type { Env } from "./env";
import { detail, json } from "./http";
import { handleRunner } from "./runner-api";
import { handleApi } from "./user-api";

export { JobBoard } from "./board";

const READ_METHODS = ["GET", "HEAD"];

function crossOriginWrite(request: Request, env: Env): boolean {
  if (READ_METHODS.includes(request.method)) return false;
  const origin = request.headers.get("origin");
  return origin !== null && origin !== env.CAIL_CANONICAL_ORIGIN;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const base = env.BASE_PATH;
    if (url.pathname === base) return new Response(null, { status: 308, headers: { location: `${base}/${url.search}` } });
    if (!url.pathname.startsWith(`${base}/`)) return detail(404, "Not Found");
    const path = url.pathname.slice(base.length);

    if (path === "/runner" || path.startsWith("/runner/")) return handleRunner(request, env, path.slice("/runner".length));
    if (path === "/health") return json({ status: "ok" });
    if (path === "/api" || path.startsWith("/api/")) {
      if (crossOriginWrite(request, env)) return detail(403, "Cross-origin request refused");
      const auth = await authenticate(request, env);
      if (!auth.ok) return auth.response;
      return handleApi(request, env, path.slice("/api".length), auth.person);
    }
    if (!READ_METHODS.includes(request.method)) return detail(405, "Method Not Allowed");
    const asset = new URL(request.url);
    asset.pathname = path;
    return env.ASSETS.fetch(new Request(asset, { method: request.method, headers: request.headers }));
  },
} satisfies ExportedHandler<Env>;
