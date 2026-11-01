/**
 * The API the React app calls, at v1's paths and with v1's responses and
 * error details. Uploads stream to R2 one file per request.
 */
import type { JobRecord } from "./board";
import { batchReport } from "./batch-report";
import {
  type AppliedChange,
  batchPartsSchema,
  type BatchParts,
  DOCUMENT_HTML_FILE,
  editRequestSchema,
  OUTPUT_READY_STATUSES,
  type ReviewAction,
  reviseRequestSchema,
} from "./contract";
import { type Env, jobBoard } from "./env";
import { attachment, detail, json, noContent, safeFilename } from "./http";
import type { Person } from "./identity";
import { appliedChanges, figureChanges, fileKey, jobResponse, readView, reviewTasks } from "./views";

export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

const DETAILS = {
  notFound: "Job not found",
  inspection: "Current output is not available for inspection",
  surface:
    "In-app review is only available after the app reaches a complete or manual-remediation output.",
  stale: "This figure decision is no longer pending review. Refresh to see the latest job state.",
  perPerson:
    "You already have too many queued or processing jobs. Wait for current jobs to finish before uploading more PDFs.",
  global: "The server is already processing the maximum number of PDFs. Try again after current jobs finish.",
  signInEnding: "Your sign-in ends soon. Sign in again, then upload the PDF.",
};

const ACTION_REPLIES = {
  undo: { status: "undone", message: "Undid the applied figure decision and restarted accessibility processing." },
  revise: { status: "reopened", message: "Retried the figure decision and restarted accessibility processing." },
  edit: { status: "edited", message: "Saved the edited figure description and restarted accessibility processing." },
};

function outputReady(job: JobRecord): boolean {
  return OUTPUT_READY_STATUSES.includes(job.status);
}

export async function handleApi(request: Request, env: Env, path: string, person: Person): Promise<Response> {
  const parts = path.split("/").filter((part) => part !== "");
  const method = request.method;
  if (parts[0] !== "jobs") return detail(404, "Not Found");
  const now = Date.now();

  if (parts.length === 1) {
    if (method === "POST") return upload(request, env, person, now);
    if (method === "GET") return listJobs(request, env, person);
    return detail(405, "Method Not Allowed");
  }
  if (parts.length === 3 && parts[1] === "download" && parts[2] === "batch-report.html" && method === "GET") {
    return downloadBatchReport(env, person, now);
  }

  const jobId = parts[1] ?? "";
  const board = jobBoard(env);
  const job = await board.getJob(person.subject, jobId);
  if (job === null) return detail(404, DETAILS.notFound);
  const rest = parts.slice(2);

  if (rest.length === 0) {
    if (method === "GET") return json(jobResponse(job, true));
    if (method === "DELETE") {
      await board.deleteJob(person.subject, jobId);
      return noContent();
    }
    return detail(405, "Method Not Allowed");
  }
  const route = rest.join("/");
  if (method === "GET") {
    if (route === "review-tasks") {
      if (!outputReady(job)) return detail(404, DETAILS.inspection);
      return json(await reviewTasks(env, job));
    }
    if (route === "applied-changes") {
      if (!outputReady(job)) return detail(409, DETAILS.surface);
      const changes = await appliedChanges(env, job);
      return json(changes.filter((change) => change.review_status === "pending_review" && change.reviewable));
    }
    if (route === "figure-changes") {
      if (!outputReady(job)) return detail(409, DETAILS.surface);
      return json(await figureChanges(env, job));
    }
    if (route === "download") {
      if (!outputReady(job) || !job.files.includes("output.pdf")) return detail(404, "Accessible PDF not yet available");
      return file(env, job, "output.pdf", attachment(`accessible_${safeFilename(job.originalFilename)}`));
    }
    if (route === "download/document.html") {
      if (!outputReady(job) || !job.files.includes(DOCUMENT_HTML_FILE)) {
        return detail(404, "Accessible HTML not yet available");
      }
      const stem = safeFilename(job.originalFilename).replace(/\.pdf$/i, "");
      return file(env, job, DOCUMENT_HTML_FILE, attachment(`accessible_${stem}.html`));
    }
    if (route === "download/report") {
      if (!outputReady(job) || !job.files.includes("views/report.json")) {
        return detail(404, "Validation report not yet available");
      }
      return file(env, job, "views/report.json", null);
    }
    if (route === "download/report.html") {
      if (!outputReady(job) || !job.files.includes("report.html")) {
        return detail(404, "Validation report not yet available");
      }
      const stem = safeFilename(job.originalFilename).replace(/\.[^.]*$/, "");
      return file(env, job, "report.html", attachment(`report_${stem}.html`));
    }
    if (rest[0] === "pages" && rest[2] === "preview" && rest.length === 3) {
      const page = Number(rest[1]);
      if (!Number.isInteger(page) || page < 1) return detail(400, "Page number must be 1 or greater");
      if (!outputReady(job)) return detail(404, DETAILS.inspection);
      if (job.pageCount !== null && page > job.pageCount) return detail(404, "Page number exceeds document length");
      if (!job.files.includes(`previews/${page}`)) return detail(404, "Preview not available for this page");
      return file(env, job, `previews/${page}`, null);
    }
    if (rest[0] === "figures" && rest[2] === "image" && rest.length === 3) {
      const name = `figures/${Number(rest[1])}`;
      if (!job.files.includes(name)) return detail(404, "Figure not found");
      return file(env, job, name, null);
    }
    if (route === "progress") return detail(404, "Progress is reported on the job itself");
    return detail(404, "Not Found");
  }

  if (method === "POST" && rest[0] === "applied-changes" && rest.length === 3) {
    return changeAction(request, env, person, job, Number(rest[1]), rest[2] ?? "", now);
  }
  if (method === "POST" && rest[0] === "review-tasks" && rest[2] === "resolve" && rest.length === 3) {
    return resolveTask(env, person, job, Number(rest[1]), now);
  }
  return detail(405, "Method Not Allowed");
}

async function file(env: Env, job: JobRecord, name: string, disposition: string | null): Promise<Response> {
  const object = await env.FILES.get(fileKey(job, name));
  if (object === null) return detail(404, "File not found");
  const headers = new Headers({ "cache-control": "private, no-store" });
  object.writeHttpMetadata(headers);
  if (name === DOCUMENT_HTML_FILE) {
    headers.set("content-type", "text/html; charset=utf-8");
    headers.set("content-security-policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'");
    headers.set("x-content-type-options", "nosniff");
  }
  if (disposition !== null) headers.set("content-disposition", disposition);
  return new Response(object.body, { headers });
}

async function listJobs(request: Request, env: Env, person: Person): Promise<Response> {
  const status = new URL(request.url).searchParams.get("status");
  const jobs = (await jobBoard(env).listJobs(person.subject)).filter((job) => status === null || job.status === status);
  return json({ jobs: jobs.map((job) => jobResponse(job, false)), total: jobs.length });
}

async function downloadBatchReport(env: Env, person: Person, now: number): Promise<Response> {
  const jobs = (await jobBoard(env).listJobs(person.subject)).filter(outputReady);
  const parts: BatchParts[] = [];
  for (const job of jobs) {
    const part = await readView(env, job, "views/batch-parts.json", batchPartsSchema);
    if (part !== null) parts.push(part);
  }
  const html = batchReport(parts, new Date(now));
  if (html === null) return detail(404, "No completed jobs to report on");
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "private, no-store",
      "content-disposition": 'attachment; filename="batch_accessibility_report.html"',
    },
  });
}

/** Read the first bytes of a stream while passing all of it on. */
async function readHead(reader: ReadableStreamDefaultReader<Uint8Array>, size: number): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (length < size) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    length += value.length;
  }
  const head = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    head.set(chunk, offset);
    offset += chunk.length;
  }
  return head;
}

async function upload(request: Request, env: Env, person: Person, now: number): Promise<Response> {
  const filename = safeFilename(new URL(request.url).searchParams.get("filename") ?? "");
  if (!filename.toLowerCase().endsWith(".pdf")) return detail(400, `File '${filename}' is not a PDF`);
  const declared = request.headers.get("content-length");
  if (declared === null) return detail(411, "Content-Length required");
  const length = Number(declared);
  if (!Number.isInteger(length) || length <= 0 || request.body === null) return detail(400, "File is empty");
  if (length > MAX_UPLOAD_BYTES) return detail(413, "File too large");

  const board = jobBoard(env);
  const admitted = await board.admit(person.subject, person.gatewayExpiresAt, now);
  if (!admitted.ok) return refused(admitted.reason);

  const reader = request.body.getReader();
  const head = await readHead(reader, 5);
  if (head.length === 0) return detail(400, "File is empty");
  if (new TextDecoder().decode(head.subarray(0, 5)) !== "%PDF-") return detail(400, "File is not a valid PDF");

  const id = crypto.randomUUID();
  const key = `jobs/${id}/input.pdf`;
  await board.reserveUpload(id, Date.now());
  const stream = new FixedLengthStream(length);
  const writer = stream.writable.getWriter();
  const pump = (async () => {
    try {
      await writer.write(head);
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        await writer.write(value);
      }
      await writer.close();
    } catch (error) {
      await Promise.allSettled([writer.abort(error)]);
      throw error;
    }
  })();
  const store = env.FILES.put(key, stream.readable, { httpMetadata: { contentType: "application/pdf" } }).catch(async (error) => {
    await Promise.allSettled([reader.cancel(error), writer.abort(error)]);
    throw error;
  });
  // Do not release the durable in-flight locator until both sides have stopped.
  const transfers = await Promise.allSettled([store, pump]);
  if (transfers.some((result) => result.status === "rejected")) {
    await board.abandonUpload(id, Date.now());
    return detail(400, "The upload did not finish. Try again.");
  }

  try {
    const stored = await board.markUploadStored(id);
    if (!stored.ok) {
      await board.abandonUpload(id, Date.now());
      return detail(stored.status, stored.detail);
    }
    const created = await board.createJob({
      id,
      owner: person.subject,
      originalFilename: filename,
      fileSizeBytes: length,
      gatewayJwt: person.gatewayJwt,
      gatewayExpiresAt: person.gatewayExpiresAt,
      now: Date.now(),
    });
    if (!created.admission.ok || created.record === null) {
      await board.abandonUpload(id, Date.now());
      return refused(created.admission.ok ? "global" : created.admission.reason);
    }
    return json({ jobs: [jobResponse(created.record, true)] }, 201);
  } catch {
    await board.abandonUpload(id, Date.now());
    return detail(503, "The upload could not be saved. Try again.");
  }
}

function refused(reason: "per_person" | "global" | "sign_in_ending"): Response {
  if (reason === "per_person") return detail(429, DETAILS.perPerson);
  if (reason === "global") return detail(503, DETAILS.global);
  return detail(401, DETAILS.signInEnding);
}

function findChange(changes: AppliedChange[], id: number): AppliedChange | null {
  return changes.find((change) => change.id === id) ?? null;
}

async function changeAction(
  request: Request,
  env: Env,
  person: Person,
  job: JobRecord,
  changeId: number,
  verb: string,
  now: number,
): Promise<Response> {
  if (!["keep", "undo", "revise", "edit"].includes(verb)) return detail(404, "Not Found");
  if (!outputReady(job)) return detail(409, DETAILS.surface);
  const change =
    findChange(await appliedChanges(env, job), changeId) ?? findChange(await figureChanges(env, job), changeId);
  if (change === null) return detail(404, "Applied change not found");
  const board = jobBoard(env);

  if (verb === "edit") {
    if (change.review_status !== "pending_review" && change.review_status !== "kept") return detail(409, DETAILS.stale);
  } else if (change.review_status !== "pending_review") {
    return detail(409, DETAILS.stale);
  }

  if (verb === "keep") {
    const updated = await board.setOverride(person.subject, job.id, "changes", changeId, now);
    return json({ status: "kept", message: "Kept this figure decision.", job_status: updated?.status ?? job.status });
  }

  let action: ReviewAction = { type: "undo", change_id: changeId };
  if (verb === "revise" || verb === "edit") {
    if (change.change_type !== "figure_semantics") {
      return detail(400, `This change type cannot be ${verb === "edit" ? "edited" : "revised"} in the app.`);
    }
  }
  if (verb === "revise") {
    const body = reviseRequestSchema.safeParse(await request.json().catch(() => null));
    if (!body.success) return detail(422, "Invalid request body");
    const feedback = body.data?.feedback?.trim();
    action = feedback ? { type: "revise", change_id: changeId, feedback } : { type: "revise", change_id: changeId };
  }
  if (verb === "edit") {
    const body = editRequestSchema.safeParse(await request.json().catch(() => null));
    if (!body.success) return detail(422, "Invalid request body");
    const text = body.data.text.trim();
    if (text === "") return detail(400, "Alt text cannot be empty.");
    action = { type: "edit", change_id: changeId, text };
  }

  const queued = await board.queueReview(person.subject, job.id, action, person.gatewayJwt, person.gatewayExpiresAt, now);
  if (!queued.ok) return detail(queued.status, queued.detail);
  const reply = ACTION_REPLIES[action.type];
  return json({ ...reply, job_status: "processing" });
}

async function resolveTask(env: Env, person: Person, job: JobRecord, taskId: number, now: number): Promise<Response> {
  if (!outputReady(job)) return detail(409, DETAILS.surface);
  const task = (await reviewTasks(env, job)).find((candidate) => candidate.id === taskId);
  if (task === undefined) return detail(404, "Review task not found");
  if (task.status === "resolved") return detail(409, "This task is already resolved.");
  const updated = await jobBoard(env).setOverride(person.subject, job.id, "tasks", taskId, now);
  return json({ status: "resolved", message: "Marked this issue as resolved.", job_status: updated?.status ?? job.status });
}
