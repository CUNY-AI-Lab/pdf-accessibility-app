/**
 * The views a runner rendered for a job's current revision, with the review
 * statuses a person set since (keep, resolve) applied on top.
 */
import type { z } from "zod";
import type { JobRecord } from "./board";
import {
  type AppliedChange,
  appliedChangesSchema,
  type JsonValue,
  type ReviewTask,
  reviewTasksSchema,
  stepsSchema,
} from "./contract";
import type { Env } from "./env";

export function fileKey(job: JobRecord, name: string): string {
  return `jobs/${job.id}/r${job.rev}/${name}`;
}

export async function readView<T>(env: Env, job: JobRecord, name: string, parser: z.ZodType<T>): Promise<T | null> {
  if (!job.files.includes(name)) return null;
  const object = await env.FILES.get(fileKey(job, name));
  if (object === null) return null;
  return parser.parse(await object.json());
}

export async function appliedChanges(env: Env, job: JobRecord): Promise<AppliedChange[]> {
  const changes = (await readView(env, job, "views/applied-changes.json", appliedChangesSchema)) ?? [];
  return changes.map((change) => ({ ...change, review_status: job.overrides.changes[String(change.id)] ?? change.review_status }));
}

export async function figureChanges(env: Env, job: JobRecord): Promise<AppliedChange[]> {
  const changes = (await readView(env, job, "views/figure-changes.json", appliedChangesSchema)) ?? [];
  return changes.map((change) => ({ ...change, review_status: job.overrides.changes[String(change.id)] ?? change.review_status }));
}

export async function reviewTasks(env: Env, job: JobRecord): Promise<ReviewTask[]> {
  const tasks = (await readView(env, job, "views/review-tasks.json", reviewTasksSchema)) ?? [];
  return tasks.map((task) => ({ ...task, status: job.overrides.tasks[String(task.id)] ?? task.status }));
}

/** v1's JobResponse. */
export function jobResponse(job: JobRecord, includeStepResults: boolean): JsonValue {
  return {
    id: job.id,
    filename: `${job.id}.pdf`,
    original_filename: job.originalFilename,
    status: job.status,
    classification: job.classification,
    ocr_language: job.ocrLanguage,
    page_count: job.pageCount,
    file_size_bytes: job.fileSizeBytes,
    error: job.error,
    notice: job.notice,
    validation_compliant: job.validationCompliant,
    created_at: new Date(job.createdAt).toISOString(),
    updated_at: new Date(job.updatedAt).toISOString(),
    steps: stepsSchema
      .parse(JSON.parse(job.stepsJson))
      .map((step) => ({ ...step, result: includeStepResults ? step.result : null })),
  };
}
