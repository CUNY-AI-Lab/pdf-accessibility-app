/**
 * JobBoard: every job, its queue position and lease, and the limits across
 * jobs, in one SQLite Durable Object.
 *
 * A job has at most one task at a time: `process` (the whole pipeline),
 * `review` (one review action and a tagging re-run), or `sync` (re-render
 * after keep or resolve). A runner claims a task with an atomic update and
 * holds it with a lease it renews by reporting progress. The lease token is
 * also the credential for that job's model relay; only its hash is stored.
 * The person's Gateway leg is kept only while the job has a task that needs
 * it.
 */
import { DurableObject } from "cloudflare:workers";
import { z } from "zod";
import {
  ACTIVE_STATUSES,
  type CompleteRequest,
  completeRequestSchema,
  type JobView,
  type Overrides,
  OUTPUT_READY_STATUSES,
  overridesSchema,
  PIPELINE_STEPS,
  type ProgressEvent,
  progressEventSchema,
  RERUN_STEPS,
  type ReviewAction,
  reviewActionSchema,
  type Step,
  stepsSchema,
} from "./contract";
import type { Env } from "./env";
import { randomToken, sha256Hex } from "./http";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
export const LIMITS = {
  activePerPerson: 3,
  activeGlobal: 12,
  attempts: 2,
  leaseMs: 3 * MINUTE,
  jobMaxMs: 3 * HOUR,
  syncMaxMs: 15 * MINUTE,
  /** Work is not queued when the person's Gateway leg has less than this left. */
  queueMinimumMs: HOUR,
  /** A queued task is not started when the leg has less than this left. */
  claimMinimumMs: 15 * MINUTE,
  /** Model calls stop this long before the leg expires. */
  gatewayMarginMs: 10 * MINUTE,
  retentionMs: 12 * HOUR,
  sweepMs: MINUTE,
};

export const MESSAGES = {
  signInEnding:
    "Your sign-in ends before this PDF could finish. Sign in again and upload the PDF again.",
  interrupted: "Processing was interrupted twice. Upload the PDF again.",
  deadline: "Processing took longer than the time limit, so it was stopped.",
};

const jobRowSchema = z.object({
  id: z.string(),
  owner: z.string(),
  original_filename: z.string(),
  file_size_bytes: z.number(),
  status: z.string(),
  error: z.string().nullable(),
  notice: z.string().nullable(),
  created_at: z.number(),
  updated_at: z.number(),
  job_json: z.string().nullable(),
  steps_json: z.string(),
  prior_json: z.string().nullable(),
  rev: z.number(),
  files_json: z.string(),
  has_state: z.number(),
  overrides_json: z.string(),
  overrides_version: z.number(),
  synced_version: z.number(),
  task_kind: z.string().nullable(),
  task_state: z.string().nullable(),
  task_action_json: z.string().nullable(),
  task_queued_at: z.number().nullable(),
  pending_review_json: z.string().nullable(),
  attempt: z.number(),
  lease_hash: z.string().nullable(),
  lease_expires: z.number().nullable(),
  deadline: z.number().nullable(),
  next_rev: z.number().nullable(),
  gateway_jwt: z.string().nullable(),
  gateway_exp: z.number().nullable(),
});
type JobRow = z.infer<typeof jobRowSchema>;

const priorSchema = z.object({ status: z.string(), steps: stepsSchema });
const pendingReviewSchema = z.object({
  action: reviewActionSchema,
  gateway_jwt: z.string(),
  gateway_exp: z.number(),
});
const filesSchema = z.array(z.string());
const jobViewJsonSchema = z.string().transform((raw, ctx) => {
  try {
    return z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])).parse(JSON.parse(raw));
  } catch {
    ctx.addIssue({ code: "custom", message: "bad job view" });
    return z.NEVER;
  }
});

/** What the Worker's routes need to answer about one job. */
export interface JobRecord {
  id: string;
  owner: string;
  originalFilename: string;
  fileSizeBytes: number;
  status: string;
  error: string | null;
  notice: string | null;
  createdAt: number;
  updatedAt: number;
  classification: string | null;
  ocrLanguage: string | null;
  pageCount: number | null;
  validationCompliant: boolean | null;
  /** The steps as JSON text: recursive step results do not cross RPC typing. */
  stepsJson: string;
  rev: number;
  files: string[];
  overrides: Overrides;
  /** A task is queued or running for the job. */
  busy: boolean;
}

export interface NewJob {
  id: string;
  owner: string;
  originalFilename: string;
  fileSizeBytes: number;
  gatewayJwt: string;
  gatewayExpiresAt: number;
  now: number;
}

export type Admission = { ok: true } | { ok: false; reason: "per_person" | "global" | "sign_in_ending" };

/** The runner's view of a task, in its wire names (backend/app/runner/client.py). */
export type ClaimedTask = {
  job_id: string;
  kind: string;
  lease: string;
  attempt: number;
  deadline: number;
  original_filename: string;
  file_size_bytes: number;
  action: ReviewAction | null;
  overrides: Overrides;
  overrides_version: number;
};

export interface Lease {
  jobId: string;
  kind: string;
  rev: number;
  nextRev: number;
  hasState: boolean;
}

export type QueueResult = { ok: true } | { ok: false; status: number; detail: string };

function pendingSteps(): Step[] {
  return PIPELINE_STEPS.map((step_name) => ({
    step_name,
    status: "pending",
    started_at: null,
    completed_at: null,
    error: null,
    result: null,
  }));
}

function parseSteps(raw: string): Step[] {
  return stepsSchema.parse(JSON.parse(raw));
}

function parseOverrides(raw: string): Overrides {
  return overridesSchema.parse(JSON.parse(raw));
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

function toRecord(row: JobRow): JobRecord {
  const view = row.job_json === null ? null : jobViewJsonSchema.parse(row.job_json);
  const text = (key: string): string | null => {
    const value = view?.[key];
    return z.string().nullable().catch(null).parse(value ?? null);
  };
  return {
    id: row.id,
    owner: row.owner,
    originalFilename: row.original_filename,
    fileSizeBytes: row.file_size_bytes,
    status: row.status,
    error: row.error,
    notice: row.notice,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    classification: text("classification"),
    ocrLanguage: text("ocr_language"),
    pageCount: z.number().nullable().catch(null).parse(view?.["page_count"] ?? null),
    validationCompliant: z.boolean().nullable().catch(null).parse(view?.["validation_compliant"] ?? null),
    stepsJson: row.steps_json,
    rev: row.rev,
    files: filesSchema.parse(JSON.parse(row.files_json)),
    overrides: parseOverrides(row.overrides_json),
    busy: row.task_kind !== null || row.pending_review_json !== null,
  };
}

/** Apply one progress event to a job's steps, as v1's _update_step does. */
export function applyProgress(steps: Step[], event: ProgressEvent, now: number): Step[] {
  if (event.step === undefined || event.status === undefined) return steps;
  const status = event.status;
  return steps.map((step) => {
    if (step.step_name !== event.step) return step;
    const finished = ["complete", "skipped", "failed"].includes(status);
    return {
      ...step,
      status,
      started_at: status === "running" ? iso(now) : step.started_at,
      completed_at: finished ? iso(now) : step.completed_at,
      error: status === "failed" ? (event.message ?? step.error) : step.error,
      result: event.result ?? step.result,
    };
  });
}

function failSteps(steps: Step[], error: string, now: number): Step[] {
  return steps.map((step) =>
    step.status === "pending" || step.status === "running"
      ? { ...step, status: "failed", completed_at: iso(now), error }
      : step,
  );
}

export class JobBoard extends DurableObject<Env> {
  private readonly sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      original_filename TEXT NOT NULL,
      file_size_bytes INTEGER NOT NULL,
      status TEXT NOT NULL,
      error TEXT,
      notice TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      job_json TEXT,
      steps_json TEXT NOT NULL,
      prior_json TEXT,
      rev INTEGER NOT NULL DEFAULT 0,
      files_json TEXT NOT NULL DEFAULT '[]',
      has_state INTEGER NOT NULL DEFAULT 0,
      overrides_json TEXT NOT NULL DEFAULT '{"changes":{},"tasks":{}}',
      overrides_version INTEGER NOT NULL DEFAULT 0,
      synced_version INTEGER NOT NULL DEFAULT 0,
      task_kind TEXT,
      task_state TEXT,
      task_action_json TEXT,
      task_queued_at INTEGER,
      pending_review_json TEXT,
      attempt INTEGER NOT NULL DEFAULT 0,
      lease_hash TEXT,
      lease_expires INTEGER,
      deadline INTEGER,
      next_rev INTEGER,
      gateway_jwt TEXT,
      gateway_exp INTEGER
    )`);
    this.sql.exec("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner, created_at)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (task_state, task_queued_at)");
  }

  private rows(query: string, ...bindings: (string | number | null)[]): JobRow[] {
    return this.sql
      .exec(query, ...bindings)
      .toArray()
      .map((row) => jobRowSchema.parse(row));
  }

  private row(id: string): JobRow | null {
    return this.rows("SELECT * FROM jobs WHERE id = ?", id)[0] ?? null;
  }

  private owned(owner: string, id: string): JobRow | null {
    const row = this.row(id);
    return row !== null && row.owner === owner ? row : null;
  }

  private async keepSweeping(now: number): Promise<void> {
    const current = await this.ctx.storage.getAlarm();
    if (current === null || current > now + LIMITS.sweepMs) await this.ctx.storage.setAlarm(now + LIMITS.sweepMs);
  }

  private activeCount(owner: string | null): number {
    const placeholders = ACTIVE_STATUSES.map(() => "?").join(", ");
    const query =
      owner === null
        ? `SELECT COUNT(*) AS n FROM jobs WHERE status IN (${placeholders})`
        : `SELECT COUNT(*) AS n FROM jobs WHERE status IN (${placeholders}) AND owner = ?`;
    const bindings = owner === null ? ACTIVE_STATUSES : [...ACTIVE_STATUSES, owner];
    return z.object({ n: z.number() }).parse(this.sql.exec(query, ...bindings).one()).n;
  }

  /** Whether a new upload may start: checked before the file is stored and again on create. */
  admit(owner: string, gatewayExpiresAt: number, now: number): Admission {
    if (gatewayExpiresAt - now < LIMITS.queueMinimumMs) return { ok: false, reason: "sign_in_ending" };
    if (this.activeCount(owner) >= LIMITS.activePerPerson) return { ok: false, reason: "per_person" };
    if (this.activeCount(null) >= LIMITS.activeGlobal) return { ok: false, reason: "global" };
    return { ok: true };
  }

  async createJob(job: NewJob): Promise<{ admission: Admission; record: JobRecord | null }> {
    const admission = this.admit(job.owner, job.gatewayExpiresAt, job.now);
    if (!admission.ok) return { admission, record: null };
    this.sql.exec(
      `INSERT INTO jobs (id, owner, original_filename, file_size_bytes, status, created_at, updated_at,
        steps_json, task_kind, task_state, task_queued_at, gateway_jwt, gateway_exp)
       VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 'process', 'queued', ?, ?, ?)`,
      job.id,
      job.owner,
      job.originalFilename,
      job.fileSizeBytes,
      job.now,
      job.now,
      JSON.stringify(pendingSteps()),
      job.now,
      job.gatewayJwt,
      job.gatewayExpiresAt,
    );
    await this.keepSweeping(job.now);
    const row = this.row(job.id);
    return { admission, record: row === null ? null : toRecord(row) };
  }

  listJobs(owner: string): JobRecord[] {
    return this.rows("SELECT * FROM jobs WHERE owner = ? ORDER BY created_at DESC", owner).map(toRecord);
  }

  getJob(owner: string, id: string): JobRecord | null {
    const row = this.owned(owner, id);
    return row === null ? null : toRecord(row);
  }

  async deleteJob(owner: string, id: string): Promise<boolean> {
    if (this.owned(owner, id) === null) return false;
    this.sql.exec("DELETE FROM jobs WHERE id = ?", id);
    await this.deleteFiles(`jobs/${id}/`);
    return true;
  }

  /** Record keep or resolve, and queue a re-render of the views and report. */
  async setOverride(owner: string, id: string, kind: "changes" | "tasks", itemId: number, now: number): Promise<JobRecord | null> {
    const row = this.owned(owner, id);
    if (row === null) return null;
    const overrides = parseOverrides(row.overrides_json);
    if (kind === "changes") overrides.changes[String(itemId)] = "kept";
    else overrides.tasks[String(itemId)] = "resolved";
    this.sql.exec(
      "UPDATE jobs SET overrides_json = ?, overrides_version = overrides_version + 1, updated_at = ? WHERE id = ?",
      JSON.stringify(overrides),
      now,
      id,
    );
    if (row.task_kind === null) this.queueTask(id, "sync", null, now);
    await this.keepSweeping(now);
    const updated = this.row(id);
    return updated === null ? null : toRecord(updated);
  }

  /** Queue undo, revise, or edit; the job shows as processing until it finishes. */
  async queueReview(owner: string, id: string, action: ReviewAction, gatewayJwt: string, gatewayExpiresAt: number, now: number): Promise<QueueResult> {
    const row = this.owned(owner, id);
    if (row === null) return { ok: false, status: 404, detail: "Job not found" };
    if (!OUTPUT_READY_STATUSES.includes(row.status) || row.task_kind === "process" || row.task_kind === "review" || row.pending_review_json !== null) {
      return {
        ok: false,
        status: 409,
        detail: "Accessibility processing is already running for this PDF. Refresh to see the latest status.",
      };
    }
    if (gatewayExpiresAt - now < LIMITS.queueMinimumMs) return { ok: false, status: 401, detail: MESSAGES.signInEnding };

    const steps = parseSteps(row.steps_json);
    const prior = JSON.stringify({ status: row.status, steps });
    const rerun = steps.map((step) =>
      RERUN_STEPS.includes(step.step_name)
        ? { ...step, status: "pending", started_at: null, completed_at: null, error: null, result: null }
        : step,
    );
    this.sql.exec(
      `UPDATE jobs SET status = 'processing', error = NULL, notice = NULL, prior_json = ?, steps_json = ?,
         gateway_jwt = ?, gateway_exp = ?, updated_at = ? WHERE id = ?`,
      prior,
      JSON.stringify(rerun),
      gatewayJwt,
      gatewayExpiresAt,
      now,
      id,
    );
    if (row.task_kind === "sync" && row.task_state === "claimed") {
      // The running re-render finishes first; this review follows it.
      this.sql.exec(
        "UPDATE jobs SET pending_review_json = ? WHERE id = ?",
        JSON.stringify({ action, gateway_jwt: gatewayJwt, gateway_exp: gatewayExpiresAt }),
        id,
      );
    } else {
      this.queueTask(id, "review", action, now);
    }
    await this.keepSweeping(now);
    return { ok: true };
  }

  private queueTask(id: string, kind: string, action: ReviewAction | null, now: number): void {
    this.sql.exec(
      `UPDATE jobs SET task_kind = ?, task_state = 'queued', task_action_json = ?, task_queued_at = ?,
         attempt = 0, lease_hash = NULL, lease_expires = NULL, deadline = NULL, next_rev = NULL WHERE id = ?`,
      kind,
      action === null ? null : JSON.stringify(action),
      now,
      id,
    );
  }

  /** The end of a task: clear it, and start whatever was waiting behind it. */
  private endTask(id: string, now: number): void {
    this.sql.exec(
      `UPDATE jobs SET task_kind = NULL, task_state = NULL, task_action_json = NULL, task_queued_at = NULL,
         attempt = 0, lease_hash = NULL, lease_expires = NULL, deadline = NULL, next_rev = NULL,
         gateway_jwt = NULL, gateway_exp = NULL,
         prior_json = CASE WHEN pending_review_json IS NULL THEN NULL ELSE prior_json END WHERE id = ?`,
      id,
    );
    const row = this.row(id);
    if (row === null) return;
    if (row.pending_review_json !== null) {
      const pending = pendingReviewSchema.parse(JSON.parse(row.pending_review_json));
      this.sql.exec(
        "UPDATE jobs SET pending_review_json = NULL, gateway_jwt = ?, gateway_exp = ? WHERE id = ?",
        pending.gateway_jwt,
        pending.gateway_exp,
        id,
      );
      this.queueTask(id, "review", pending.action, now);
    } else if (row.overrides_version > row.synced_version && OUTPUT_READY_STATUSES.includes(row.status)) {
      this.queueTask(id, "sync", null, now);
    }
  }

  /** Give the oldest queued task to a runner. */
  async claim(now: number): Promise<ClaimedTask | null> {
    for (;;) {
      const row = this.rows(
        "SELECT * FROM jobs WHERE task_state = 'queued' ORDER BY task_kind = 'process', task_queued_at LIMIT 1",
      )[0];
      if (row === undefined) return null;
      const kind = row.task_kind ?? "process";
      const needsGateway = kind !== "sync";
      const gatewayExp = row.gateway_exp ?? 0;
      if (needsGateway && gatewayExp - now < LIMITS.claimMinimumMs) {
        this.failRow(row, MESSAGES.signInEnding, now);
        continue;
      }
      const lease = randomToken();
      const deadline = needsGateway
        ? Math.min(now + LIMITS.jobMaxMs, gatewayExp - LIMITS.gatewayMarginMs)
        : now + LIMITS.syncMaxMs;
      const status = kind === "process" ? "processing" : row.status;
      this.sql.exec(
        `UPDATE jobs SET task_state = 'claimed', attempt = attempt + 1, lease_hash = ?, lease_expires = ?,
           deadline = ?, next_rev = rev + 1, status = ?, updated_at = ? WHERE id = ? AND task_state = 'queued'`,
        await sha256Hex(lease),
        now + LIMITS.leaseMs,
        deadline,
        status,
        now,
        row.id,
      );
      await this.keepSweeping(now);
      return {
        job_id: row.id,
        kind,
        lease,
        attempt: row.attempt + 1,
        deadline,
        original_filename: row.original_filename,
        file_size_bytes: row.file_size_bytes,
        action: row.task_action_json === null ? null : reviewActionSchema.parse(JSON.parse(row.task_action_json)),
        overrides: parseOverrides(row.overrides_json),
        overrides_version: row.overrides_version,
      };
    }
  }

  private async heldRow(id: string, lease: string, now: number): Promise<JobRow | null> {
    const row = this.row(id);
    if (row === null || row.task_state !== "claimed" || row.lease_hash === null) return null;
    if ((row.lease_expires ?? 0) < now || (row.deadline ?? 0) < now) return null;
    return row.lease_hash === (await sha256Hex(lease)) ? row : null;
  }

  /** Check a runner's lease for a job, renewing it when asked. */
  async checkLease(id: string, lease: string, now: number, renew: boolean): Promise<Lease | null> {
    const row = await this.heldRow(id, lease, now);
    if (row === null) return null;
    if (renew) this.sql.exec("UPDATE jobs SET lease_expires = ?, updated_at = ? WHERE id = ?", now + LIMITS.leaseMs, now, id);
    return {
      jobId: id,
      kind: row.task_kind ?? "process",
      rev: row.rev,
      nextRev: row.next_rev ?? row.rev + 1,
      hasState: row.has_state === 1,
    };
  }

  /** The person's Gateway leg, for a model call the job's runner makes. */
  async relayCredential(id: string, lease: string, now: number): Promise<string | null> {
    const row = await this.heldRow(id, lease, now);
    if (row === null || row.task_kind === "sync" || row.gateway_jwt === null) return null;
    if ((row.gateway_exp ?? 0) - LIMITS.gatewayMarginMs < now) return null;
    return row.gateway_jwt;
  }

  async progress(id: string, lease: string, eventJson: string, now: number): Promise<boolean> {
    const row = await this.heldRow(id, lease, now);
    if (row === null) return false;
    const event = progressEventSchema.parse(JSON.parse(eventJson));
    const steps = row.task_kind === "sync" ? parseSteps(row.steps_json) : applyProgress(parseSteps(row.steps_json), event, now);
    this.sql.exec(
      "UPDATE jobs SET steps_json = ?, lease_expires = ?, updated_at = ? WHERE id = ?",
      JSON.stringify(steps),
      now + LIMITS.leaseMs,
      now,
      id,
    );
    return true;
  }

  async complete(id: string, lease: string, resultJson: string, now: number): Promise<boolean> {
    const row = await this.heldRow(id, lease, now);
    if (row === null) return false;
    const result: CompleteRequest = completeRequestSchema.parse(JSON.parse(resultJson));
    if ("review_refused" in result) {
      const prior = row.prior_json === null ? null : priorSchema.parse(JSON.parse(row.prior_json));
      this.sql.exec(
        "UPDATE jobs SET status = ?, steps_json = ?, notice = ?, updated_at = ? WHERE id = ?",
        prior?.status ?? row.status,
        JSON.stringify(prior?.steps ?? parseSteps(row.steps_json)),
        result.review_refused,
        now,
        id,
      );
      this.endTask(id, now);
      return true;
    }
    const files = [...result.files, ...(result.has_state ? ["state.tar.gz"] : [])];
    // A review queued while this re-render ran keeps the job processing; the
    // fresh result becomes what a refused review returns to.
    const waiting = row.pending_review_json !== null;
    const finished = { status: result.job.status, steps: result.job.steps };
    this.sql.exec(
      `UPDATE jobs SET status = ?, error = ?, job_json = ?, steps_json = ?, prior_json = ?, rev = ?, files_json = ?,
         has_state = ?, synced_version = ?, updated_at = ? WHERE id = ?`,
      waiting ? row.status : result.job.status,
      result.job.error,
      JSON.stringify(viewSummary(result.job)),
      waiting ? row.steps_json : JSON.stringify(result.job.steps),
      waiting ? JSON.stringify(finished) : row.prior_json,
      row.next_rev ?? row.rev + 1,
      JSON.stringify(files),
      result.has_state ? 1 : 0,
      result.overrides_version,
      now,
      id,
    );
    this.endTask(id, now);
    if (row.rev > 0) await this.deleteFiles(`jobs/${id}/r${row.rev}/`);
    return true;
  }

  async fail(id: string, lease: string, error: string, now: number): Promise<boolean> {
    const row = await this.heldRow(id, lease, now);
    if (row === null) return false;
    if (row.task_kind === "sync") this.endTask(id, now);
    else this.failRow(row, error, now);
    return true;
  }

  private failRow(row: JobRow, error: string, now: number): void {
    this.sql.exec(
      "UPDATE jobs SET status = 'failed', error = ?, steps_json = ?, updated_at = ? WHERE id = ?",
      error,
      JSON.stringify(failSteps(parseSteps(row.steps_json), error, now)),
      now,
      row.id,
    );
    this.sql.exec("UPDATE jobs SET pending_review_json = NULL WHERE id = ?", row.id);
    this.endTask(row.id, now);
  }

  /** Requeue or fail tasks whose runner went quiet, end overdue ones, and delete old jobs. */
  override async alarm(): Promise<void> {
    const now = Date.now();
    await this.sweep(now);
    const remaining = z.object({ n: z.number() }).parse(this.sql.exec("SELECT COUNT(*) AS n FROM jobs").one()).n;
    if (remaining > 0) await this.ctx.storage.setAlarm(now + LIMITS.sweepMs);
  }

  async sweep(now: number): Promise<void> {
    for (const row of this.rows("SELECT * FROM jobs WHERE task_state = 'claimed'")) {
      if ((row.deadline ?? 0) < now) {
        if (row.task_kind === "sync") this.endTask(row.id, now);
        else this.failRow(row, MESSAGES.deadline, now);
      } else if ((row.lease_expires ?? 0) < now) {
        if (row.task_kind === "sync") this.endTask(row.id, now);
        else if (row.attempt < LIMITS.attempts) {
          this.sql.exec(
            `UPDATE jobs SET task_state = 'queued', task_queued_at = ?, lease_hash = NULL, lease_expires = NULL,
               deadline = NULL, next_rev = NULL, status = CASE WHEN task_kind = 'process' THEN 'queued' ELSE status END,
               steps_json = ? WHERE id = ?`,
            now,
            JSON.stringify(row.task_kind === "process" ? pendingSteps() : parseSteps(row.steps_json)),
            row.id,
          );
        } else this.failRow(row, MESSAGES.interrupted, now);
      }
    }
    for (const row of this.rows("SELECT * FROM jobs WHERE updated_at < ?", now - LIMITS.retentionMs)) {
      this.sql.exec("DELETE FROM jobs WHERE id = ?", row.id);
      await this.deleteFiles(`jobs/${row.id}/`);
    }
  }

  private async deleteFiles(prefix: string): Promise<void> {
    let cursor: string | undefined;
    do {
      const listing = await this.env.FILES.list(cursor === undefined ? { prefix } : { prefix, cursor });
      if (listing.objects.length > 0) await this.env.FILES.delete(listing.objects.map((object) => object.key));
      cursor = listing.truncated ? listing.cursor : undefined;
    } while (cursor !== undefined);
  }
}

/** The fields of the runner's JobResponse that the Worker does not own. */
type ViewSummary = Pick<JobView, "classification" | "ocr_language" | "page_count" | "validation_compliant">;

function viewSummary(job: JobView): ViewSummary {
  return {
    classification: job.classification,
    ocr_language: job.ocr_language,
    page_count: job.page_count,
    validation_compliant: job.validation_compliant,
  };
}
