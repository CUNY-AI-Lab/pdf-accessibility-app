/**
 * The runner's API at /pdf-accessibility/runner, reached through Doorway's
 * public route. Every call carries the host credential (x-pdf-runner-token);
 * calls about a task carry its lease (x-pdf-job-token), which is also the
 * credential for that job's model relay. Lease problems answer 409 and an
 * unknown job 404, which the runner treats as a lost task.
 */
import {
  claimRequestSchema,
  completeRequestSchema,
  failRequestSchema,
  isRunnerFileName,
  progressEventSchema,
} from "./contract";
import { type Env, jobBoard } from "./env";
import { detail, json, noContent, sameSecret } from "./http";

export const RUNNER_TOKEN_HEADER = "x-pdf-runner-token";
export const JOB_TOKEN_HEADER = "x-pdf-job-token";
const MAX_RUNNER_FILE_BYTES = 500 * 1024 * 1024;
const RELAYED_HEADERS = ["content-type", "retry-after", "x-should-retry"];

const leaseLost = (): Response => detail(409, "This task is no longer held by the runner");

export async function handleRunner(request: Request, env: Env, path: string): Promise<Response> {
  if (!(await sameSecret(request.headers.get(RUNNER_TOKEN_HEADER) ?? "", env.RUNNER_TOKEN))) {
    return detail(401, "Runner credential required");
  }
  const now = Date.now();
  const board = jobBoard(env);
  const parts = path.split("/").filter((part) => part !== "");

  if (parts.length === 1 && parts[0] === "claim" && request.method === "POST") {
    const body = claimRequestSchema.safeParse(await request.json().catch(() => null));
    if (!body.success) return detail(422, "Invalid claim");
    const task = await board.claim(now);
    if (task === null) return noContent();
    console.log(JSON.stringify({ event: "task_claimed", job: task.job_id, kind: task.kind, attempt: task.attempt, runner: body.data.runner }));
    return new Response(JSON.stringify({ task }), {
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  }

  if (parts[0] !== "tasks" || parts.length < 3) return detail(404, "Not Found");
  const jobId = parts[1] ?? "";
  const lease = request.headers.get(JOB_TOKEN_HEADER) ?? "";
  if (lease === "") return leaseLost();
  const action = parts.slice(2).join("/");

  if (action === "v1/chat/completions" && request.method === "POST") {
    const credential = await board.relayCredential(jobId, lease, now);
    if (credential === null) return leaseLost();
    const upstream = await env.GATEWAY.fetch(
      new Request(`${env.CAIL_API_BASE}/v1/chat/completions`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-cail-identity-jwt": credential },
        body: request.body,
      }),
    );
    const headers = new Headers({ "cache-control": "no-store" });
    for (const name of RELAYED_HEADERS) {
      const value = upstream.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers });
  }

  if (action === "progress" && request.method === "POST") {
    const event = progressEventSchema.safeParse(await request.json().catch(() => null));
    if (!event.success) return detail(422, "Invalid progress event");
    return (await board.progress(jobId, lease, JSON.stringify(event.data), now)) ? json({ ok: true }) : leaseLost();
  }

  if (action === "complete" && request.method === "POST") {
    const result = completeRequestSchema.safeParse(await request.json().catch(() => null));
    if (!result.success) return detail(422, "Invalid completion");
    return (await board.complete(jobId, lease, JSON.stringify(result.data), now)) ? noContent() : leaseLost();
  }

  if (action === "fail" && request.method === "POST") {
    const failure = failRequestSchema.safeParse(await request.json().catch(() => null));
    if (!failure.success) return detail(422, "Invalid failure");
    return (await board.fail(jobId, lease, failure.data.error, now)) ? noContent() : leaseLost();
  }

  const held = await board.checkLease(jobId, lease, now, true);
  if (held === null) return leaseLost();

  if ((action === "input" || action === "state") && request.method === "GET") {
    const key = action === "input" ? `jobs/${jobId}/input.pdf` : `jobs/${jobId}/r${held.rev}/state.tar.gz`;
    if (action === "state" && !held.hasState) return detail(404, "This job has no saved state");
    const object = await env.FILES.get(key);
    if (object === null) return detail(404, "File not found");
    return new Response(object.body, { headers: { "content-type": "application/octet-stream" } });
  }

  if (parts[2] === "files" && request.method === "PUT") {
    const name = parts.slice(3).join("/");
    if (!isRunnerFileName(name)) return detail(400, "Unexpected file name");
    const length = Number(request.headers.get("content-length") ?? "");
    if (!Number.isInteger(length) || length < 0 || length > MAX_RUNNER_FILE_BYTES || request.body === null) {
      return detail(413, "File missing or too large");
    }
    await env.FILES.put(`jobs/${jobId}/r${held.nextRev}/${name}`, request.body, {
      httpMetadata: { contentType: request.headers.get("content-type") ?? "application/octet-stream" },
    });
    return noContent();
  }
  return detail(404, "Not Found");
}
