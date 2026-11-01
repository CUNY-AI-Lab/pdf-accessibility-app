import type { JsonValue } from "./contract";

const NO_STORE = { "cache-control": "private, no-store" };

export function json(body: JsonValue, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...NO_STORE, "content-type": "application/json" },
  });
}

/** The API error body read by the React app. */
export function detail(status: number, message: string): Response {
  return json({ detail: message }, status);
}

export function noContent(): Response {
  return new Response(null, { status: 204, headers: NO_STORE });
}

/** v1's path_safety.safe_filename: the last path component, or a default. */
export function safeFilename(name: string): string {
  const last = name.replaceAll("\\", "/").split("/").pop()?.trim() ?? "";
  return last === "" || last === "." || last === ".." ? "document.pdf" : last;
}

/** v1's attachment_content_disposition. */
export function attachment(filename: string): string {
  const name = safeFilename(filename);
  let fallback = name
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^[._-]+|[._-]+$/g, "")
    .replace(/_+(\.[A-Za-z0-9]+)$/, "$1");
  if (fallback === "") fallback = "download";
  return `attachment; filename="${fallback}"; filename*=UTF-8''${encodeURIComponent(name).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)}`;
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function sameSecret(given: string, expected: string): Promise<boolean> {
  if (expected === "") return false;
  const [a, b] = await Promise.all([sha256Hex(given), sha256Hex(expected)]);
  const left = new TextEncoder().encode(a);
  const right = new TextEncoder().encode(b);
  return crypto.subtle.timingSafeEqual(left, right);
}
