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
  jobStatusSchema,
  type Overrides,
  OUTPUT_READY_STATUSES,
  overridesSchema,
  PIPELINE_STEPS,
  type ProgressEvent,
  progressEventSchema,
  type ReviewAction,
  reviewActionSchema,
  type Step,
  stepsSchema,
} from "./contract";
import { guardArtifactTransition, guardRetentionTransition, guardUploadTransition } from "./artifacts";
import { CREDENTIAL_LIMITS, credentialEligibility } from "./credential";
import { applyStepProgress, resetSteps, validateSteps, guardStepTransition } from "./steps";
import { isRunnerFileName } from "./contract";
import type { Env } from "./env";
import { randomToken, sha256Hex } from "./http";
import { type FailureReason, jobFinished, parseReportChecks, parseReviewTasks } from "./outcome";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
export const LIMITS = {
  activePerPerson: 3,
  activeGlobal: 12,
  attempts: 2,
  leaseMs: 3 * MINUTE,
  jobMaxMs: 3 * HOUR,
  syncMaxMs: 15 * MINUTE,
  ...CREDENTIAL_LIMITS,
  retentionMs: 12 * HOUR,
  sweepMs: MINUTE,
};

export const MESSAGES = {
  signInEnding:
    "Your sign-in ends before this PDF could finish. Sign in again and upload the PDF again.",
  interrupted: "Processing was interrupted twice. Upload the PDF again.",
  deadline: "Processing took longer than the time limit, so it was stopped.",
  inconsistent: "This PDF's processing record was inconsistent, so it was stopped. Upload the PDF again.",
};

const jobRowSchema = z.object({
  id: z.string(),
  owner: z.string(),
  original_filename: z.string(),
  file_size_bytes: z.number(),
  // Stored rows parse leniently; transitions are guarded where they are written.
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
/**
 * Keep and resolve, stored as the overrides version each was recorded at. A
 * task's views include every override up to the version it was claimed with;
 * when it completes those give way to the views, and only newer ones stay
 * layered on top.
 */
const storedOverridesSchema = z.object({
  changes: z.record(z.string(), z.number()),
  tasks: z.record(z.string(), z.number()),
});
type StoredOverrides = z.infer<typeof storedOverridesSchema>;
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
  attempt_id?: string;
};

export interface Lease {
  jobId: string;
  kind: string;
  rev: number;
  nextRev: number;
  hasState: boolean;
}

export type QueueResult = { ok: true } | { ok: false; status: number; detail: string };

/** Guards run synchronously with the SQL writes; never await after reading a row. */
const TASK_TRANSITIONS = {
  queue: ["idle", "queued"],
  claim: ["queued"],
  renew: ["claimed"],
  complete: ["claimed"],
  refuse: ["claimed"],
  retry: ["claimed"],
  end: ["queued", "claimed"],
};
type TaskEvent = keyof typeof TASK_TRANSITIONS;

function taskTransitionError(row: JobRow, event: TaskEvent): string | null {
  const state = row.task_state ?? "idle";
  if (event !== "end" && event !== "queue" && !["process", "review", "sync"].includes(row.task_kind ?? "")) return `Illegal task kind: ${row.task_kind}`;
  if (!TASK_TRANSITIONS[event].includes(state)) return `Illegal task transition: ${state} -> ${event}`;
  if (event === "refuse" && row.task_kind !== "review") return "Only a review task may report review_refused";
  if (event === "retry" && row.attempt >= LIMITS.attempts) return "Task retry limit reached";
  if (event === "queue" && row.task_kind !== null && row.task_kind !== "sync") return "Cannot replace an active process or review task";
  return null;
}

function guardTaskTransition(row: JobRow, event: TaskEvent): void {
  const error = taskTransitionError(row, event);
  if (error !== null) throw new Error(error);
}

const JOB_TRANSITIONS = {
  queued: ["processing", "failed"],
  processing: ["queued", "processing", "complete", "manual_remediation", "failed"],
  complete: ["processing", "complete", "manual_remediation", "failed"],
  manual_remediation: ["processing", "complete", "manual_remediation", "failed"],
  failed: ["failed"],
};

function guardJobTransition(row: JobRow, status: string): void {
  // A status outside the vocabulary can only be failed out of, never resumed.
  const current = jobStatusSchema.safeParse(row.status);
  const allowed: readonly string[] = current.success ? JOB_TRANSITIONS[current.data] : ["failed"];
  if (status === row.status && current.success) return;
  if (!jobStatusSchema.safeParse(status).success || !allowed.includes(status)) {
    throw new Error(`Illegal job transition: ${row.status} -> ${status}`);
  }
}

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

function parseStoredOverrides(raw: string): StoredOverrides {
  return storedOverridesSchema.parse(JSON.parse(raw));
}

function parseOverrides(raw: string): Overrides {
  const stored = parseStoredOverrides(raw);
  return overridesSchema.parse({
    changes: Object.fromEntries(Object.keys(stored.changes).map((id) => [id, "kept"])),
    tasks: Object.fromEntries(Object.keys(stored.tasks).map((id) => [id, "resolved"])),
  });
}

function overridesAfter(stored: StoredOverrides, version: number): StoredOverrides {
  const newer = (entries: { [id: string]: number }) =>
    Object.fromEntries(Object.entries(entries).filter(([, recorded]) => recorded > version));
  return { changes: newer(stored.changes), tasks: newer(stored.tasks) };
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
  return applyStepProgress(steps, event, now);
}

function failSteps(steps: Step[], error: string, now: number): Step[] {
  return steps.map((step) =>
    step.status === "pending" || step.status === "running"
      ? (guardStepTransition(step.status, "failed"), { ...step, status: "failed", completed_at: iso(now), error })
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
    // A lease is the immutable attempt identity already sent by b0d9b79.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS completions (
      job_id TEXT NOT NULL, lease_hash TEXT NOT NULL, result_json TEXT NOT NULL,
      PRIMARY KEY (job_id, lease_hash)
    )`);
    const migratingArtifacts = this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'artifact_attempts'").toArray().length === 0;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS artifact_attempts (
      job_id TEXT NOT NULL, rev INTEGER NOT NULL, lease_hash TEXT NOT NULL, state TEXT NOT NULL,
      PRIMARY KEY(job_id, rev)
    )`);
    if (migratingArtifacts) {
      // Old Workers reused rev+1. Move their allocation beyond all old revisions,
      // including abandoned revisions no longer named by a job row.
      this.sql.exec("INSERT INTO artifact_attempts SELECT id, MAX(rev, COALESCE(next_rev, 0), ?), 'migration', 'abandoned' FROM jobs", Date.now());
      this.sql.exec("INSERT OR IGNORE INTO artifact_attempts SELECT id, next_rev, lease_hash, 'staging' FROM jobs WHERE task_state = 'claimed' AND next_rev IS NOT NULL AND lease_hash IS NOT NULL");
    }
    this.sql.exec(`CREATE TABLE IF NOT EXISTS cleanup (
      prefix TEXT PRIMARY KEY, state TEXT NOT NULL, not_before INTEGER NOT NULL
    )`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS upload_writes (id TEXT PRIMARY KEY, prefix TEXT NOT NULL)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, expires INTEGER NOT NULL, state TEXT NOT NULL)`);
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
    if (credentialEligibility("process", "admission", gatewayExpiresAt, now) !== "queue_eligible") return { ok: false, reason: "sign_in_ending" };
    if (this.activeCount(owner) >= LIMITS.activePerPerson) return { ok: false, reason: "per_person" };
    if (this.activeCount(null) >= LIMITS.activeGlobal) return { ok: false, reason: "global" };
    return { ok: true };
  }

  async createJob(job: NewJob): Promise<{ admission: Admission; record: JobRecord | null }> {
    await this.keepSweeping(job.now);
    const now = Math.max(job.now, Date.now());
    const reservation = this.sql.exec("SELECT expires, state FROM uploads WHERE id = ?", job.id).toArray()[0];
    if (reservation === undefined || z.object({ expires: z.number() }).parse(reservation).expires <= now) {
      return { admission: { ok: false, reason: "sign_in_ending" }, record: null };
    }
    if (this.sql.exec("SELECT prefix FROM cleanup WHERE prefix = ?", `jobs/${job.id}/`).toArray().length > 0) {
      return { admission: { ok: false, reason: "global" }, record: null };
    }
    const uploadState = z.object({ state: z.string() }).parse(reservation).state;
    guardUploadTransition(uploadState, "admitted");
    const admission = this.admit(job.owner, job.gatewayExpiresAt, now);
    if (!admission.ok) return { admission, record: null };
    this.sql.exec(
      `INSERT INTO jobs (id, owner, original_filename, file_size_bytes, status, created_at, updated_at,
        steps_json, task_kind, task_state, task_queued_at, gateway_jwt, gateway_exp)
       VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 'process', 'queued', ?, ?, ?)`,
      job.id,
      job.owner,
      job.originalFilename,
      job.fileSizeBytes,
      now,
      now,
      JSON.stringify(pendingSteps()),
      now,
      job.gatewayJwt,
      job.gatewayExpiresAt,
    );
    this.sql.exec("UPDATE uploads SET state = 'admitted' WHERE id = ?", job.id);
    this.sql.exec("DELETE FROM uploads WHERE id = ?", job.id);
    this.sql.exec("DELETE FROM upload_writes WHERE id = ?", job.id);
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
    await this.keepSweeping(Date.now());
    this.requestCleanup(`jobs/${id}/`, Date.now());
    this.sql.exec("DELETE FROM completions WHERE job_id = ?", id);
    this.sql.exec("DELETE FROM jobs WHERE id = ?", id);
    this.sql.exec("DELETE FROM artifact_attempts WHERE job_id = ?", id);
    await this.cleanFiles(Date.now());
    return true;
  }

  /** Record keep or resolve, and queue a re-render of the views and report. */
  async setOverride(owner: string, id: string, kind: "changes" | "tasks", itemId: number, now: number): Promise<JobRecord | null> {
    const row = this.owned(owner, id);
    if (row === null) return null;
    const overrides = parseStoredOverrides(row.overrides_json);
    const version = row.overrides_version + 1;
    if (kind === "changes") overrides.changes[String(itemId)] = version;
    else overrides.tasks[String(itemId)] = version;
    this.sql.exec(
      "UPDATE jobs SET overrides_json = ?, overrides_version = ?, updated_at = ? WHERE id = ?",
      JSON.stringify(overrides),
      version,
      now,
      id,
    );
    if (row.task_kind === null && row.has_state === 1) this.queueTask(id, "sync", null, now);
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
    if (credentialEligibility("review", gatewayJwt, gatewayExpiresAt, now) !== "queue_eligible") return { ok: false, status: 401, detail: MESSAGES.signInEnding };
    if (row.has_state !== 1) {
      return { ok: false, status: 409, detail: "This PDF can no longer be changed in the app. Upload it again." };
    }

    guardJobTransition(row, "processing");
    const steps = parseSteps(row.steps_json);
    const prior = JSON.stringify({ status: row.status, steps });
    const rerun = resetSteps(steps, true);
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
    const row = this.row(id);
    if (row === null) throw new Error("Cannot queue a missing job");
    guardTaskTransition(row, "queue");
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
  private endTask(id: string, now: number, dirtySync = true): void {
    const current = this.row(id);
    if (current === null) throw new Error("Cannot end a missing task");
    guardTaskTransition(current, "end");
    const artifact = this.sql.exec("SELECT state FROM artifact_attempts WHERE job_id = ? AND rev = ?", id, current.next_rev).toArray()[0];
    if (artifact?.["state"] === "staging") {
      this.artifactTransition(id, current.next_rev, "abandoned");
      this.requestCleanup(`jobs/${id}/r${current.next_rev}/`, now + 5 * MINUTE);
    }
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
    } else if (
      dirtySync && row.overrides_version > row.synced_version &&
      row.has_state === 1 &&
      OUTPUT_READY_STATUSES.includes(row.status)
    ) {
      this.queueTask(id, "sync", null, now);
    }
  }

  /** Give the oldest queued task to a runner. */
  async claim(now: number): Promise<ClaimedTask | null> {
    const lease = randomToken();
    const leaseHash = await sha256Hex(lease);
    for (;;) {
      const row = this.rows(
        "SELECT * FROM jobs WHERE task_state = 'queued' ORDER BY task_kind = 'process', task_queued_at LIMIT 1",
      )[0];
      if (row === undefined) return null;
      const kind = row.task_kind ?? "process";
      const needsGateway = kind !== "sync";
      const gatewayExp = row.gateway_exp ?? 0;
      if (needsGateway && !["queue_eligible", "claim_eligible"].includes(credentialEligibility(kind, row.gateway_jwt, row.gateway_exp, now))) {
        this.failRow(row, MESSAGES.signInEnding, "sign_in_ending", now);
        continue;
      }
      const deadline = needsGateway
        ? Math.min(now + LIMITS.jobMaxMs, gatewayExp - LIMITS.gatewayMarginMs)
        : now + LIMITS.syncMaxMs;
      const status = kind === "process" ? "processing" : row.status;
      // One bad row must not stall the queue: retire it and claim the next.
      try {
        guardTaskTransition(row, "claim");
        guardJobTransition(row, status);
      } catch (error) {
        this.failRow(row, MESSAGES.inconsistent, "runner", now);
        console.error(`Retired job ${row.id} at claim: ${String(error)}`);
        continue;
      }
      const highest = z.object({ rev: z.number() }).parse(this.sql.exec(
        "SELECT MAX(rev) AS rev FROM (SELECT rev FROM artifact_attempts WHERE job_id = ? UNION ALL SELECT ? AS rev UNION ALL SELECT ? AS rev)",
        row.id, row.rev, row.next_rev ?? 0,
      ).one()).rev;
      const nextRev = highest + 1;
      this.sql.exec("INSERT INTO artifact_attempts VALUES (?, ?, ?, 'staging')", row.id, nextRev, leaseHash);
      this.sql.exec(
        `UPDATE jobs SET task_state = 'claimed', attempt = attempt + 1, lease_hash = ?, lease_expires = ?,
           deadline = ?, next_rev = ?, status = ?, updated_at = ? WHERE id = ? AND task_state = 'queued'`,
        leaseHash,
        now + LIMITS.leaseMs,
        deadline,
        nextRev,
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
        attempt_id: leaseHash,
      };
    }
  }

  private heldRow(id: string, leaseHash: string, now: number): JobRow | null {
    const row = this.row(id);
    if (row === null || row.task_state !== "claimed" || row.lease_hash !== leaseHash) return null;
    if ((row.lease_expires ?? 0) <= now || (row.deadline ?? 0) <= now) return null;
    return row;
  }

  /** Check a runner's lease for a job, renewing it when asked. */
  async checkLease(id: string, lease: string, now: number, renew: boolean): Promise<Lease | null> {
    const leaseHash = await sha256Hex(lease);
    const row = this.heldRow(id, leaseHash, now);
    if (row === null) return null;
    guardTaskTransition(row, "renew");
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
    const leaseHash = await sha256Hex(lease);
    const row = this.heldRow(id, leaseHash, now);
    if (row === null || row.task_kind === "sync" || row.gateway_jwt === null) return null;
    if (!["queue_eligible", "claim_eligible", "relay_only"].includes(credentialEligibility(row.task_kind ?? "process", row.gateway_jwt, row.gateway_exp, now))) return null;
    return row.gateway_jwt;
  }

  async progress(id: string, lease: string, eventJson: string, now: number): Promise<QueueResult> {
    const leaseHash = await sha256Hex(lease);
    const row = this.heldRow(id, leaseHash, now);
    if (row === null) return { ok: false, status: 409, detail: "This task is no longer held by the runner" };
    guardTaskTransition(row, "renew");
    const event = progressEventSchema.parse(JSON.parse(eventJson));
    let steps: Step[];
    try {
      steps = row.task_kind === "sync" ? parseSteps(row.steps_json) : applyProgress(parseSteps(row.steps_json), event, now);
    } catch (error) {
      return { ok: false, status: 422, detail: String(error) };
    }
    this.sql.exec(
      "UPDATE jobs SET steps_json = ?, lease_expires = ?, updated_at = ? WHERE id = ?",
      JSON.stringify(steps),
      now + LIMITS.leaseMs,
      now,
      id,
    );
    return { ok: true };
  }

  async complete(id: string, lease: string, resultJson: string, now: number): Promise<QueueResult> {
    const parsed = completeRequestSchema.safeParse(JSON.parse(resultJson));
    if (!parsed.success) return { ok: false, status: 422, detail: "Completion requires a defined terminal job status" };
    const result: CompleteRequest = parsed.data;
    const canonical = JSON.stringify(result);
    const leaseHash = await sha256Hex(lease);
    const receipt = this.sql.exec("SELECT result_json FROM completions WHERE job_id = ? AND lease_hash = ?", id, leaseHash).toArray()[0];
    if (receipt !== undefined) {
      return receipt["result_json"] === canonical
        ? { ok: true }
        : { ok: false, status: 409, detail: "Conflicting completion for this attempt" };
    }
    let row = this.heldRow(id, leaseHash, now);
    if (row === null) return { ok: false, status: 409, detail: "This task is no longer held by the runner" };
    const error = taskTransitionError(row, "review_refused" in result ? "refuse" : "complete");
    if (error !== null) return { ok: false, status: 409, detail: error };
    if ("review_refused" in result) {
      const prior = row.prior_json === null ? null : priorSchema.parse(JSON.parse(row.prior_json));
      if (prior === null || !OUTPUT_READY_STATUSES.includes(prior.status)) {
        return { ok: false, status: 409, detail: "Review refusal requires a prior terminal result" };
      }
      guardJobTransition(row, prior.status);
      this.sql.exec(
        "UPDATE jobs SET status = ?, steps_json = ?, notice = ?, updated_at = ? WHERE id = ?",
        prior?.status ?? row.status,
        JSON.stringify(prior?.steps ?? parseSteps(row.steps_json)),
        result.review_refused,
        now,
        id,
      );
      this.recordCompletion(id, leaseHash, canonical);
      this.endTask(id, now);
      return { ok: true };
    }
    const files = [...new Set([...result.files, ...(result.has_state ? ["state.tar.gz"] : [])])];
    try {
      validateSteps(result.job.steps);
    } catch (error) {
      return { ok: false, status: 422, detail: String(error) };
    }
    if (files.some((name) => !isRunnerFileName(name))) return { ok: false, status: 422, detail: "Invalid artifact manifest" };
    const prefix = `jobs/${id}/r${row.next_rev ?? row.rev + 1}/`;
    const objects = await Promise.all(files.map((name) => this.env.FILES.head(`${prefix}${name}`)));
    if (objects.some((object) => object === null)) return { ok: false, status: 422, detail: "Artifact manifest is incomplete" };
    // Re-read ownership after R2 I/O. Publication below has no await until the pointer and receipt are committed.
    const current = this.heldRow(id, leaseHash, Math.max(now, Date.now()));
    const racedReceipt = this.sql.exec("SELECT result_json FROM completions WHERE job_id = ? AND lease_hash = ?", id, leaseHash).toArray()[0];
    if (racedReceipt !== undefined) return racedReceipt["result_json"] === canonical ? { ok: true } : { ok: false, status: 409, detail: "Conflicting completion for this attempt" };
    if (current === null) return { ok: false, status: 409, detail: "This task is no longer held by the runner" };
    row = current;
    this.artifactTransition(id, row.next_rev, "sealed");
    // A review queued while this re-render ran keeps the job processing; the
    // fresh result becomes what a refused review returns to.
    const waiting = row.pending_review_json !== null;
    const finished = { status: result.job.status, steps: result.job.steps };
    guardJobTransition(row, waiting ? row.status : result.job.status);
    this.sql.exec(
      `UPDATE jobs SET status = ?, error = ?, job_json = ?, steps_json = ?, prior_json = ?, rev = ?, files_json = ?,
         has_state = ?, synced_version = ?, overrides_json = ?, updated_at = ? WHERE id = ?`,
      waiting ? row.status : result.job.status,
      result.job.error,
      JSON.stringify(viewSummary(result.job)),
      waiting ? row.steps_json : JSON.stringify(result.job.steps),
      waiting ? JSON.stringify(finished) : row.prior_json,
      row.next_rev ?? row.rev + 1,
      JSON.stringify(files),
      result.has_state ? 1 : 0,
      result.overrides_version,
      JSON.stringify(overridesAfter(parseStoredOverrides(row.overrides_json), result.overrides_version)),
      now,
      id,
    );
    this.artifactTransition(id, row.next_rev, "published");
    this.recordCompletion(id, leaseHash, canonical);
    this.endTask(id, now);
    // Readers that captured the previous pointer get a grace period.
    if (row.rev > 0) this.requestCleanup(`jobs/${id}/r${row.rev}/`, now + 5 * MINUTE);
    if (row.task_kind !== "sync") {
      try {
        await this.logFinished(row, result.job, `jobs/${id}/r${row.next_rev ?? row.rev + 1}/`, files, now);
      } catch {
        console.error(JSON.stringify({ event: "outcome_log_failed", job: id }));
      }
    }
    return { ok: true };
  }

  async beginArtifactUpload(id: string, lease: string, now: number): Promise<string | null> {
    const hash = await sha256Hex(lease);
    const row = this.heldRow(id, hash, now);
    if (row === null) return null;
    const attempt = this.sql.exec("SELECT state FROM artifact_attempts WHERE job_id = ? AND rev = ?", id, row.next_rev).toArray()[0];
    if (attempt !== undefined && attempt["state"] !== "staging") return null;
    const write = randomToken();
    this.sql.exec("INSERT INTO upload_writes VALUES (?, ?)", write, `jobs/${id}/r${row.next_rev}/`);
    return write;
  }

  async endArtifactUpload(write: string, id: string, lease: string, now: number): Promise<void> {
    const hash = await sha256Hex(lease);
    const entry = this.sql.exec("SELECT prefix FROM upload_writes WHERE id = ?", write).toArray()[0];
    this.sql.exec("DELETE FROM upload_writes WHERE id = ?", write);
    if (entry !== undefined && this.heldRow(id, hash, now) === null) {
      const receipt = this.sql.exec("SELECT job_id FROM completions WHERE job_id = ? AND lease_hash = ?", id, hash).toArray()[0];
      if (receipt === undefined) {
        this.requestCleanup(z.object({ prefix: z.string() }).parse(entry).prefix, now);
        await this.keepSweeping(now);
      }
    }
  }

  private artifactTransition(id: string, rev: number | null, target: "sealed" | "published" | "abandoned"): void {
    const stored = this.sql.exec("SELECT state FROM artifact_attempts WHERE job_id = ? AND rev = ?", id, rev).toArray()[0];
    // A lease already held at upgrade used the deployed runner's staging layout.
    if (stored === undefined) return;
    const state = z.object({ state: z.string() }).parse(stored).state;
    guardArtifactTransition(state, target);
    this.sql.exec("UPDATE artifact_attempts SET state = ? WHERE job_id = ? AND rev = ?", target, id, rev);
  }

  private requestCleanup(prefix: string, notBefore: number): void {
    const existing = this.sql.exec("SELECT state FROM cleanup WHERE prefix = ?", prefix).toArray()[0];
    if (existing !== undefined) return;
    guardRetentionTransition("retained", "deletion_requested");
    this.sql.exec("INSERT INTO cleanup VALUES (?, 'deletion_requested', ?)", prefix, notBefore);
  }

  async reserveUpload(id: string, now: number): Promise<void> {
    await this.keepSweeping(now);
    this.sql.exec("INSERT INTO uploads VALUES (?, ?, 'receiving')", id, now + LIMITS.leaseMs);
    this.sql.exec("INSERT INTO upload_writes VALUES (?, ?)", id, `jobs/${id}/`);
  }

  markUploadStored(id: string): QueueResult {
    const row = this.sql.exec("SELECT state FROM uploads WHERE id = ?", id).toArray()[0];
    if (row === undefined) return { ok: false, status: 409, detail: "Upload reservation expired" };
    guardUploadTransition(z.object({ state: z.string() }).parse(row).state, "stored");
    this.sql.exec("UPDATE uploads SET state = 'stored' WHERE id = ?", id);
    return { ok: true };
  }

  async abandonUpload(id: string, now: number): Promise<void> {
    // An RPC acknowledgement can be lost after createJob committed. The input
    // now belongs to that admitted job, even if the upload request saw an error.
    if (this.row(id) !== null) return;
    const row = this.sql.exec("SELECT state FROM uploads WHERE id = ?", id).toArray()[0];
    if (row !== undefined) {
      guardUploadTransition(z.object({ state: z.string() }).parse(row).state, "abandoned");
    }
    this.sql.exec("DELETE FROM uploads WHERE id = ?", id);
    this.sql.exec("DELETE FROM upload_writes WHERE id = ?", id);
    this.requestCleanup(`jobs/${id}/`, now);
    await this.keepSweeping(now);
    await this.cleanFiles(now);
  }

  private async cleanFiles(now: number): Promise<void> {
    const pending = this.sql.exec("SELECT prefix, state FROM cleanup WHERE not_before <= ?", now).toArray();
    for (const item of pending) {
      const entry = z.object({ prefix: z.string(), state: z.string() }).parse(item);
      guardRetentionTransition(entry.state, "deleting");
      this.sql.exec("UPDATE cleanup SET state = 'deleting' WHERE prefix = ?", entry.prefix);
      try {
        await this.deleteFiles(entry.prefix);
        const outstanding = this.sql.exec("SELECT id FROM upload_writes WHERE substr(prefix, 1, length(?)) = ? LIMIT 1", entry.prefix, entry.prefix).toArray();
        if (outstanding.length === 0) {
          guardRetentionTransition("deleting", "deleted");
          this.sql.exec("UPDATE cleanup SET state = 'deleted' WHERE prefix = ?", entry.prefix);
          this.sql.exec("DELETE FROM cleanup WHERE prefix = ?", entry.prefix);
        }
      } catch {
        guardRetentionTransition("deleting", "delete_failed");
        this.sql.exec("UPDATE cleanup SET state = 'delete_failed' WHERE prefix = ?", entry.prefix);
      }
    }
  }

  private recordCompletion(id: string, leaseHash: string, result: string): void {
    this.sql.exec("INSERT INTO completions (job_id, lease_hash, result_json) VALUES (?, ?, ?)", id, leaseHash, result);
  }

  /**
   * The job_finished event for a task that returned a result (src/outcome.ts),
   * from the runner's job view and the review tasks and gate checks it
   * uploaded with it. The event is a report: a view that cannot be read
   * leaves its part empty and does not change what the runner is told.
   */
  private async logFinished(row: JobRow, job: JobView, prefix: string, files: string[], now: number): Promise<void> {
    const read = async (name: string): Promise<string | null> => {
      if (!files.includes(name)) return null;
      try {
        return (await (await this.env.FILES.get(`${prefix}${name}`))?.text()) ?? null;
      } catch {
        return null;
      }
    };
    const [tasks, report] = await Promise.all([read("views/review-tasks.json"), read("views/report.json")]);
    const event = jobFinished(
      {
        jobId: row.id,
        kind: row.task_kind ?? "process",
        attempt: row.attempt,
        since: taskStart(row),
        status: job.status,
        failure: job.status === "failed" ? "pipeline" : null,
        classification: job.classification,
        pages: job.page_count,
        validationCompliant: job.validation_compliant,
        steps: job.steps,
        stepsBefore: job.steps,
        reviewTasks: tasks === null ? [] : parseReviewTasks(tasks),
        checks: report === null ? [] : parseReportChecks(report),
      },
      now,
    );
    logOutcome(row, event);
  }

  async fail(id: string, lease: string, error: string, now: number): Promise<boolean> {
    const leaseHash = await sha256Hex(lease);
    const row = this.heldRow(id, leaseHash, now);
    if (row === null) return false;
    if (row.task_kind === "sync") this.retrySync(row, now);
    else this.failRow(row, error, "runner", now);
    return true;
  }

  private failRow(row: JobRow, error: string, reason: FailureReason, now: number): void {
    guardTaskTransition(row, "end");
    guardJobTransition(row, "failed");
    const before = parseSteps(row.steps_json);
    const steps = failSteps(before, error, now);
    this.sql.exec(
      "UPDATE jobs SET status = 'failed', error = ?, steps_json = ?, updated_at = ? WHERE id = ?",
      error,
      JSON.stringify(steps),
      now,
      row.id,
    );
    this.sql.exec("UPDATE jobs SET pending_review_json = NULL WHERE id = ?", row.id);
    this.endTask(row.id, now);
    // The error is the person's message and may name their document; the
    // event keeps only the reason.
    const record = toRecord(row);
    const event = jobFinished(
      {
        jobId: row.id,
        kind: row.task_kind ?? "process",
        attempt: row.attempt,
        since: taskStart(row),
        status: "failed",
        failure: reason,
        classification: record.classification,
        pages: record.pageCount,
        validationCompliant: null,
        steps,
        stepsBefore: before,
        reviewTasks: [],
        checks: [],
      },
      now,
    );
    logOutcome(row, event);
  }

  /** Requeue or fail tasks whose runner went quiet, end overdue ones, and delete old jobs. */
  override async alarm(): Promise<void> {
    const now = Date.now();
    await this.sweep(now);
    const remaining = z.object({ n: z.number() }).parse(this.sql.exec("SELECT (SELECT COUNT(*) FROM jobs) + (SELECT COUNT(*) FROM cleanup) + (SELECT COUNT(*) FROM uploads) AS n").one()).n;
    if (remaining > 0) await this.ctx.storage.setAlarm(now + LIMITS.sweepMs);
  }

  async sweep(now: number): Promise<void> {
    for (const row of this.rows("SELECT * FROM jobs WHERE task_state = 'claimed'")) {
      if ((row.deadline ?? 0) <= now) {
        if (row.task_kind === "sync") this.stopSync(row, now);
        else this.failRow(row, MESSAGES.deadline, "deadline", now);
      } else if ((row.lease_expires ?? 0) <= now) {
        if (row.task_kind === "sync") this.retrySync(row, now);
        else if (row.attempt < LIMITS.attempts) {
          this.requeue(row, now);
        } else this.failRow(row, MESSAGES.interrupted, "interrupted", now);
      }
    }
    for (const upload of this.sql.exec("SELECT id FROM uploads WHERE expires <= ?", now).toArray()) {
      const id = z.object({ id: z.string() }).parse(upload).id;
      const state = this.sql.exec("SELECT state FROM uploads WHERE id = ?", id).toArray()[0];
      guardUploadTransition(z.object({ state: z.string() }).parse(state).state, "abandoned");
      this.requestCleanup(`jobs/${id}/`, now);
      this.sql.exec("DELETE FROM uploads WHERE id = ?", id);
    }
    for (const row of this.rows("SELECT * FROM jobs WHERE updated_at <= ?", now - LIMITS.retentionMs)) {
      this.requestCleanup(`jobs/${row.id}/`, now);
      this.sql.exec("DELETE FROM completions WHERE job_id = ?", row.id);
      this.sql.exec("DELETE FROM jobs WHERE id = ?", row.id);
      this.sql.exec("DELETE FROM artifact_attempts WHERE job_id = ?", row.id);
    }
    await this.cleanFiles(now);
  }

  private requeue(row: JobRow, now: number): void {
    guardTaskTransition(row, "retry");
    this.artifactTransition(row.id, row.next_rev, "abandoned");
    if (row.next_rev !== null) this.requestCleanup(`jobs/${row.id}/r${row.next_rev}/`, now + 5 * MINUTE);
    const status = row.task_kind === "process" ? "queued" : row.status;
    guardJobTransition(row, status);
    this.sql.exec(
      `UPDATE jobs SET task_state = 'queued', task_queued_at = ?, lease_hash = NULL, lease_expires = NULL,
         deadline = NULL, next_rev = NULL, status = ?, steps_json = ? WHERE id = ?`,
      now, status, JSON.stringify(row.task_kind === "sync" ? parseSteps(row.steps_json) : resetSteps(parseSteps(row.steps_json), row.task_kind === "review")), row.id,
    );
  }

  private retrySync(row: JobRow, now: number): void {
    if (row.pending_review_json !== null || row.attempt >= LIMITS.attempts) this.stopSync(row, now);
    else this.requeue(row, now);
  }

  private stopSync(row: JobRow, now: number): void {
    this.sql.exec("UPDATE jobs SET notice = ? WHERE id = ?", "Report refresh was interrupted. Your review choices are saved.", row.id);
    // Keep the published result and unsynced overlays, but do not restart the exhausted task.
    this.endTask(row.id, now, false);
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

/** Reporting cannot turn a committed outcome into a failed operation. */
function logOutcome(row: JobRow, event: ReturnType<typeof jobFinished>): void {
  try {
    console.log(JSON.stringify({ ...event, attempt_id: row.lease_hash, result_id: row.lease_hash === null ? null : `${row.id}/${row.lease_hash}/result` }));
  } catch {
    return;
  }
}

/** When the person asked for the task: the upload for a process task, the request for a review. */
function taskStart(row: JobRow): number {
  return row.task_kind === "process" ? row.created_at : (row.task_queued_at ?? row.created_at);
}

function viewSummary(job: JobView): ViewSummary {
  return {
    classification: job.classification,
    ocr_language: job.ocr_language,
    page_count: job.page_count,
    validation_compliant: job.validation_compliant,
  };
}
