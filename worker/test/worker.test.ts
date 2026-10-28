import { canonicalTestSubject } from "@cuny-ai-lab/cail-identity/testing";
import { runInDurableObject, SELF } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";
import { z } from "zod";
import { LIMITS, MESSAGES } from "../src/board";
import { jobBoard } from "../src/env";
import {
  alice,
  api,
  bob,
  CHANGES,
  claim,
  createdJobId,
  finishedJob,
  finishProcess,
  job,
  ORIGIN,
  PDF,
  putFile,
  runner,
  signedIn,
  upload,
} from "./helpers";

const relayedSchema = z.object({
  seen: z.object({ url: z.string(), credential: z.string(), authorization: z.string().nullable() }),
});

beforeEach(async () => {
  await runInDurableObject(jobBoard(env), (_board, state) => {
    state.storage.sql.exec("DELETE FROM jobs");
  });
  const stored = await env.FILES.list({ prefix: "jobs/" });
  if (stored.objects.length > 0) await env.FILES.delete(stored.objects.map((object) => object.key));
});

describe("sign-in", () => {
  it("refuses the API without Doorway's identity legs", async () => {
    const response = await api("/jobs", { as: new Headers() });
    expect(response.status).toBe(401);
  });

  it("refuses an app leg without the Gateway leg", async () => {
    const headers = await signedIn();
    headers.delete("x-cail-gateway-identity-jwt");
    expect((await api("/jobs", { as: headers })).status).toBe(401);
  });

  it("refuses a Gateway leg that belongs to someone else", async () => {
    const mine = await signedIn(alice);
    const theirs = await signedIn(bob);
    mine.set("x-cail-gateway-identity-jwt", theirs.get("x-cail-gateway-identity-jwt") ?? "");
    expect((await api("/jobs", { as: mine })).status).toBe(401);
  });

  it("refuses writes from another origin", async () => {
    const response = await api("/jobs?filename=a.pdf", {
      method: "POST",
      headers: { origin: "https://evil.example", "content-length": String(PDF.length) },
      body: PDF,
    });
    expect(response.status).toBe(403);
  });

  it("refuses runner calls without the runner credential", async () => {
    const response = await runner("/claim", {
      method: "POST",
      headers: { "x-pdf-runner-token": "wrong" },
      body: JSON.stringify({ runner: "x" }),
    });
    expect(response.status).toBe(401);
  });
});

describe("uploads", () => {
  it("accepts a PDF and queues it", async () => {
    const id = await createdJobId(await upload());
    const created = await job(id);
    expect(created.status).toBe("queued");
    expect(created.original_filename).toBe("syllabus.pdf");
    expect(created.steps.every((step) => step.status === "pending")).toBe(true);
    const stored = await env.FILES.get(`jobs/${id}/input.pdf`);
    expect(new Uint8Array(await (stored ?? new Response("")).arrayBuffer())).toEqual(PDF);
  });

  it("refuses bytes that are not a PDF and stores nothing", async () => {
    const response = await upload(undefined, "fake.pdf", new TextEncoder().encode("hello world"));
    expect(response.status).toBe(400);
    expect((await env.FILES.list({ prefix: "jobs/" })).objects).toHaveLength(0);
  });

  it("refuses a fourth active job for one person but not for another", async () => {
    for (let i = 0; i < LIMITS.activePerPerson; i += 1) expect((await upload()).status).toBe(201);
    expect((await upload()).status).toBe(429);
    expect((await upload(await signedIn(bob))).status).toBe(201);
  });

  it("refuses work when the Gateway leg is about to expire", async () => {
    const response = await upload(await signedIn(alice, 30 * 60));
    expect(response.status).toBe(401);
  });

  it("keeps each person's jobs to themselves", async () => {
    const id = await createdJobId(await upload());
    const asBob = await signedIn(bob);
    expect((await api(`/jobs/${id}`, { as: asBob })).status).toBe(404);
    expect((await api(`/jobs/${id}`, { method: "DELETE", as: asBob, headers: { origin: ORIGIN } })).status).toBe(404);
    const listed = await (await api("/jobs", { as: asBob })).json();
    expect(listed).toEqual({ jobs: [], total: 0 });
  });
});

describe("the runner", () => {
  it("takes a job, relays its model calls as the person, and returns the result", async () => {
    const person = await signedIn();
    const id = await createdJobId(await upload(person));
    const task = await claim();
    expect(task?.job_id).toBe(id);
    if (task === null) return;
    expect(task.kind).toBe("process");
    expect(await claim()).toBeNull();
    expect((await job(id)).status).toBe("processing");

    const input = await runner(`/tasks/${id}/input`, { lease: task.lease });
    expect(new Uint8Array(await input.arrayBuffer())).toEqual(PDF);

    const progress = await runner(`/tasks/${id}/progress`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({ step: "classify", status: "running" }),
    });
    expect(progress.status).toBe(200);
    expect((await job(id)).steps.find((step) => step.step_name === "classify")?.status).toBe("running");

    const relayed = await runner(`/tasks/${id}/v1/chat/completions`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({ model: "qwen3-vl", messages: [] }),
    });
    expect(relayed.status).toBe(200);
    const { seen } = relayedSchema.parse(await relayed.json());
    expect(seen.url).toBe(`${ORIGIN}/v1/chat/completions`);
    expect(seen.authorization).toBeNull();
    expect(seen.credential).toBe(person.get("x-cail-gateway-identity-jwt"));

    expect((await finishProcess(task)).status).toBe(204);
    const finished = await job(id);
    expect(finished.status).toBe("manual_remediation");
    expect(finished.page_count).toBe(4);

    const download = await api(`/jobs/${id}/download`);
    expect(download.status).toBe(200);
    expect(download.headers.get("content-disposition")).toContain("accessible_syllabus.pdf");
    expect(new TextDecoder().decode(await download.arrayBuffer())).toBe("%PDF-1.7 tagged");
    expect((await api(`/jobs/${id}/pages/2/preview`)).status).toBe(200);
    expect((await api(`/jobs/${id}/pages/3/preview`)).status).toBe(404);
    expect((await api(`/jobs/${id}/figures/0/image`)).status).toBe(200);
    const batch = await api("/jobs/download/batch-report.html");
    expect(await batch.text()).toContain('<div class="job-section">syllabus</div>');
  });

  it("passes the Gateway's refusal through unchanged", async () => {
    await upload();
    const task = await claim();
    if (task === null) throw new Error("no task");
    const refused = await runner(`/tasks/${task.job_id}/v1/chat/completions`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({ model: "budget-spent", messages: [] }),
    });
    expect(refused.status).toBe(429);
    expect(refused.headers.get("x-should-retry")).toBe("false");
  });

  it("refuses a wrong lease, and refuses everything once the job is deleted", async () => {
    const id = await createdJobId(await upload());
    const task = await claim();
    if (task === null) throw new Error("no task");
    const wrong = await runner(`/tasks/${id}/v1/chat/completions`, {
      method: "POST",
      lease: "not-the-lease",
      body: JSON.stringify({ model: "m", messages: [] }),
    });
    expect(wrong.status).toBe(409);

    expect((await api(`/jobs/${id}`, { method: "DELETE", headers: { origin: ORIGIN } })).status).toBe(204);
    const after = await runner(`/tasks/${id}/progress`, { method: "POST", lease: task.lease, body: "{}" });
    expect(after.status).toBe(409);
    expect((await env.FILES.list({ prefix: `jobs/${id}/` })).objects).toHaveLength(0);
  });

  it("does not accept files outside the revision's names", async () => {
    await upload();
    const task = await claim();
    if (task === null) throw new Error("no task");
    const response = await runner(`/tasks/${task.job_id}/files/views/anything.json`, {
      method: "PUT",
      lease: task.lease,
      body: "x",
    });
    expect(response.status).toBe(400);
    expect((await env.FILES.list({ prefix: `jobs/${task.job_id}/r1/` })).objects).toHaveLength(0);
  });

  it("asks for the length of a file it would store", async () => {
    await upload();
    const task = await claim();
    if (task === null) throw new Error("no task");
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("{}"));
        controller.close();
      },
    });
    const response = await runner(`/tasks/${task.job_id}/files/views/report.json`, {
      method: "PUT",
      lease: task.lease,
      body: stream,
    });
    expect(response.status).toBe(411);
  });

  it("fails a job whose Gateway leg would expire before the work could start", async () => {
    const id = await createdJobId(await upload());
    await runInDurableObject(jobBoard(env), (_board, state) => {
      state.storage.sql.exec("UPDATE jobs SET gateway_exp = ? WHERE id = ?", Date.now() + 5 * 60_000, id);
    });
    expect(await claim()).toBeNull();
    const failed = await job(id);
    expect(failed.status).toBe("failed");
    expect(failed.error).toBe(MESSAGES.signInEnding);
  });
});

describe("leases and retention", () => {
  it("retries a job whose runner went quiet once, then fails it", async () => {
    const id = await createdJobId(await upload());
    const board = jobBoard(env);
    const first = await claim();
    expect(first?.attempt).toBe(1);
    await board.sweep(Date.now() + LIMITS.leaseMs + 1000);
    expect((await job(id)).status).toBe("queued");

    const second = await claim();
    expect(second?.attempt).toBe(2);
    if (second === null) return;
    const stale = await runner(`/tasks/${id}/progress`, { method: "POST", lease: first?.lease ?? "", body: "{}" });
    expect(stale.status).toBe(409);
    await board.sweep(Date.now() + LIMITS.leaseMs + 1000);
    const failed = await job(id);
    expect(failed.status).toBe("failed");
    expect(failed.error).toBe(MESSAGES.interrupted);
  });

  it("deletes a job and its files after the retention period", async () => {
    const id = await createdJobId(await upload());
    await jobBoard(env).sweep(Date.now() + LIMITS.retentionMs + 1000);
    expect((await api(`/jobs/${id}`)).status).toBe(404);
    expect((await env.FILES.list({ prefix: `jobs/${id}/` })).objects).toHaveLength(0);
  });
});

describe("review", () => {
  async function reviewable(): Promise<string> {
    const id = await createdJobId(await upload());
    const task = await claim();
    if (task === null) throw new Error("no task");
    await finishProcess(task);
    return id;
  }

  it("keeps a change at once and re-renders the views with it", async () => {
    const id = await reviewable();
    const kept = await api(`/jobs/${id}/applied-changes/1/keep`, { method: "POST", headers: { origin: ORIGIN } });
    expect(kept.status).toBe(200);
    expect(await (await api(`/jobs/${id}/applied-changes`)).json()).toEqual([]);
    const again = await api(`/jobs/${id}/applied-changes/1/keep`, { method: "POST", headers: { origin: ORIGIN } });
    expect(again.status).toBe(409);

    const sync = await claim();
    expect(sync?.kind).toBe("sync");
    expect(sync?.overrides.changes).toEqual({ "1": "kept" });
    expect((await job(id)).status).toBe("manual_remediation");
  });

  it("lets the runner's later view replace a status it has already applied", async () => {
    const id = await reviewable();
    await api(`/jobs/${id}/applied-changes/1/keep`, { method: "POST", headers: { origin: ORIGIN } });
    const sync = await claim();
    if (sync === null) throw new Error("no sync task");
    expect(sync.overrides_version).toBe(1);
    // The runner applied the keep, then (say) an edit marked the change undone.
    const undone = [{ ...CHANGES[0], review_status: "undone" }];
    await putFile(sync, "views/figure-changes.json", JSON.stringify(undone), "application/json");
    const done = await runner(`/tasks/${id}/complete`, {
      method: "POST",
      lease: sync.lease,
      body: JSON.stringify({
        job: finishedJob("manual_remediation"),
        figures: [],
        previews: [],
        files: ["views/figure-changes.json"],
        has_state: false,
        overrides_version: sync.overrides_version,
      }),
    });
    expect(done.status).toBe(204);
    const figures = z.array(z.object({ review_status: z.string() })).parse(await (await api(`/jobs/${id}/figure-changes`)).json());
    expect(figures[0]?.review_status).toBe("undone");
  });

  it("resolves a task once", async () => {
    const id = await reviewable();
    const resolve = () => api(`/jobs/${id}/review-tasks/7/resolve`, { method: "POST", headers: { origin: ORIGIN } });
    expect((await resolve()).status).toBe(200);
    expect((await resolve()).status).toBe(409);
    const tasks = z.array(z.object({ status: z.string() })).parse(await (await api(`/jobs/${id}/review-tasks`)).json());
    expect(tasks[0]?.status).toBe("resolved");
  });

  it("queues an edit as a re-run and returns to the result when the runner refuses it", async () => {
    const id = await reviewable();
    const edited = await api(`/jobs/${id}/applied-changes/1/edit`, {
      method: "POST",
      headers: { origin: ORIGIN, "content-type": "application/json" },
      body: JSON.stringify({ text: "Enrollment by year" }),
    });
    expect(edited.status).toBe(200);
    expect((await job(id)).status).toBe("processing");
    expect((await api(`/jobs/${id}/applied-changes`)).status).toBe(409);

    const task = await claim();
    expect(task?.kind).toBe("review");
    expect(task?.action).toEqual({ type: "edit", change_id: 1, text: "Enrollment by year" });
    if (task === null) return;
    const refused = await runner(`/tasks/${id}/complete`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({ review_refused: "No longer pending.", overrides_version: 0 }),
    });
    expect(refused.status).toBe(204);
    const after = await job(id);
    expect(after.status).toBe("manual_remediation");
    expect(after.notice).toBe("No longer pending.");
    expect(after.steps.find((step) => step.step_name === "tagging")?.status).toBe("complete");
  });

  it("runs an undo after a re-render that was already under way", async () => {
    const id = await reviewable();
    await api(`/jobs/${id}/review-tasks/7/resolve`, { method: "POST", headers: { origin: ORIGIN } });
    const sync = await claim();
    if (sync === null) throw new Error("no sync task");
    const undo = await api(`/jobs/${id}/applied-changes/1/undo`, { method: "POST", headers: { origin: ORIGIN } });
    expect(undo.status).toBe(200);
    expect(await claim()).toBeNull();

    expect((await finishProcess(sync)).status).toBe(204);
    expect((await job(id)).status).toBe("processing");
    const review = await claim();
    expect(review?.kind).toBe("review");
    expect(review?.action?.type).toBe("undo");
  });

  it("refuses a change it could not apply when the job has no saved state", async () => {
    const id = await reviewable();
    await runInDurableObject(jobBoard(env), (_board, state) => {
      state.storage.sql.exec("UPDATE jobs SET has_state = 0 WHERE id = ?", id);
    });
    const undo = await api(`/jobs/${id}/applied-changes/1/undo`, { method: "POST", headers: { origin: ORIGIN } });
    expect(undo.status).toBe(409);
    await api(`/jobs/${id}/applied-changes/1/keep`, { method: "POST", headers: { origin: ORIGIN } });
    expect(await claim()).toBeNull();
    expect((await job(id)).status).toBe("manual_remediation");
  });

  it("refuses review before a result exists", async () => {
    const id = await createdJobId(await upload());
    const response = await api(`/jobs/${id}/applied-changes/1/undo`, { method: "POST", headers: { origin: ORIGIN } });
    expect(response.status).toBe(409);
  });
});

describe("the app", () => {
  it("serves the React app under the mount path for any page route", async () => {
    const page = await SELF.fetch(`${ORIGIN}/pdf-accessibility/jobs/${canonicalTestSubject("any")}`);
    expect(page.status).toBe(200);
    expect(await page.text()).toContain("<div id=\"root\">");
  });
});
