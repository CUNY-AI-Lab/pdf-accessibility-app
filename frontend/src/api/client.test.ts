import { afterEach, expect, it, vi } from "vitest";
import { apiFetch, ApiError } from "./client";

afterEach(() => vi.unstubAllGlobals());
function browser() {
  const assign = vi.fn();
  vi.stubGlobal("document", { cookie: "" });
  vi.stubGlobal("window", { location: { assign } });
  return { assign };
}
it("returns unknown stored status values without narrowing or rejecting a read", async () => {
  browser();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "legacy-future" })));
  expect(await apiFetch("/jobs/example")).toEqual({ status: "legacy-future" });
});
it("handles a no-content deletion reply without parsing JSON", async () => {
  browser();
  const fetch = vi.fn(async (url: string) => url.endsWith("/health") ? Response.json({ status: "ok" }) : new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  expect(await apiFetch("/jobs/example", { method: "DELETE" })).toBeUndefined();
  expect(fetch.mock.calls.filter(([url]) => url.endsWith("/jobs/example"))).toHaveLength(1);
});
it("redirects only a Doorway authentication reply and reports an API error", async () => {
  const { assign } = browser();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { message: "session ended", launch: "/launch/pdf" } }, { status: 401 })));
  await expect(apiFetch("/jobs")).rejects.toMatchObject({ status: 401, detail: "session ended" });
  expect(assign).toHaveBeenCalledWith("/launch/pdf");
});
it("does not retry an ordinary v2 refusal and preserves the upload body", async () => {
  const { assign } = browser();
  const body = new File(["PDF"], "a.pdf");
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/health")) return Response.json({ status: "ok" });
    expect(options?.body).toBe(body);
    return Response.json({ detail: "refused" }, { status: 403 });
  });
  vi.stubGlobal("fetch", fetch);
  await expect(apiFetch("/jobs", { method: "POST", body })).rejects.toBeInstanceOf(ApiError);
  expect(fetch.mock.calls.filter(([url]) => url.endsWith("/api/jobs"))).toHaveLength(1);
  expect(assign).not.toHaveBeenCalled();
});
