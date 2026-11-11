/**
 * The `job_finished` log event: one structured line in Workers Observability
 * when a process or review task ends with a result or a failure. It holds
 * what the runner reported (the job's view, its review tasks, its release
 * gate checks) reduced to statuses, counts, ids and timings. It never holds
 * a file name, a person, an error message, or any text or title from the
 * document. `dev/outcomes.ts` reads these events back.
 */
import { z } from "zod";
import { PIPELINE_STEPS, RERUN_STEPS, type ReviewTask, reviewTasksSchema, type Step } from "./contract";

export const JOB_FINISHED = "job_finished";

/** A release gate check holds a document when it fails or cannot run. */
const HOLDING_STATUSES = ["fail", "error"];

const checksSchema = z.array(z.object({ check: z.string(), status: z.string() }));
export type GateCheck = z.infer<typeof checksSchema>[number];
const reportSchema = z.object({ fidelity: z.object({ checks: checksSchema }) });

/** A view the runner uploaded, or null when it is not JSON of that shape. */
function parseView<T>(raw: string, parser: z.ZodType<T>): T | null {
  try {
    const parsed = parser.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

/** The gate checks in the runner's views/report.json (the fidelity report). */
export function parseReportChecks(raw: string): GateCheck[] {
  return parseView(raw, reportSchema)?.fidelity.checks ?? [];
}

/** The review tasks in the runner's views/review-tasks.json. */
export function parseReviewTasks(raw: string): ReviewTask[] {
  return parseView(raw, reviewTasksSchema) ?? [];
}

/** What a step result reports of its model calls (backend llm_operations.empty_llm_usage). */
const usageSchema = z.object({
  request_count: z.number(),
  prompt_tokens: z.number(),
  completion_tokens: z.number(),
  cost_usd: z.number(),
});
const classifyResultSchema = z.object({ type: z.string() });
const fidelityResultSchema = z.object({ blocking_tasks: z.number(), advisory_tasks: z.number() });

const modelUsageSchema = z.object({
  requests: z.number(),
  prompt_tokens: z.number(),
  completion_tokens: z.number(),
  cost_usd: z.number(),
});
export type ModelUsage = z.infer<typeof modelUsageSchema>;
const taskCountSchema = z.object({ total: z.number(), blocking: z.number() });

/** The event as logged, and as dev/outcomes.ts reads it back. */
export const jobFinishedSchema = z.object({
  event: z.literal(JOB_FINISHED),
  job: z.string(),
  kind: z.string(),
  attempt: z.number(),
  status: z.string(),
  /**
   * Why a job failed: the pipeline failed and the runner reported its
   * result, the runner gave up on the task (a refused upload, an internal
   * error, its own deadline), or the Worker ended it (deadline, runner gone
   * twice, sign-in ending before the work could start).
   */
  failure: z.enum(["pipeline", "runner", "deadline", "interrupted", "sign_in_ending"]).nullable(),
  failed_step: z.string().nullable(),
  classification: z.string().nullable(),
  pages: z.number().nullable(),
  /** From the upload (process) or the review request (review) to the end. */
  seconds: z.number(),
  steps: z.record(z.string(), z.object({ status: z.string(), seconds: z.number().nullable() })),
  validation_compliant: z.boolean().nullable(),
  /** The whole backlog's counts, including tasks the review screen does not show. */
  blocking_tasks: z.number().nullable(),
  advisory_tasks: z.number().nullable(),
  /** The tasks the review screen shows, by type. */
  review_tasks: z.record(z.string(), taskCountSchema),
  /**
   * Blocking tasks the review screen does not show (their types are not in
   * the backend's review_surface.USER_VISIBLE_REVIEW_TASK_TYPES): they hold
   * the document, and the person cannot see or resolve them.
   */
  hidden_blocking_tasks: z.number().nullable(),
  /** The release gate checks that failed or could not run. */
  held_by: z.array(z.string()),
  model_usage: modelUsageSchema.nullable(),
});
export type JobFinished = z.infer<typeof jobFinishedSchema>;
export type FailureReason = NonNullable<JobFinished["failure"]>;

/** A finished task as the JobBoard knows it. */
export interface FinishedTask {
  jobId: string;
  kind: string;
  attempt: number;
  since: number;
  status: string;
  failure: FailureReason | null;
  classification: string | null;
  pages: number | null;
  validationCompliant: boolean | null;
  /** The steps as the task left them. */
  steps: Step[];
  /** The steps before the Worker marked a failure on them, to find the one that was running. */
  stepsBefore: Step[];
  reviewTasks: ReviewTask[];
  checks: GateCheck[];
}

function seconds(from: string | null, to: string | null): number | null {
  if (from === null || to === null) return null;
  const elapsed = Date.parse(to) - Date.parse(from);
  return Number.isFinite(elapsed) ? Math.round(elapsed / 100) / 10 : null;
}

/** The step a failure stopped: the one running, or else the first not reached. */
function failedStep(steps: Step[]): string | null {
  const stopped = steps.find((step) => step.status === "running" || step.status === "failed");
  return (stopped ?? steps.find((step) => step.status === "pending"))?.step_name ?? null;
}

/** Model use as the steps report it; top-level `*llm_usage` keys already sum their parts. */
function modelUsage(steps: Step[]): ModelUsage | null {
  let total: ModelUsage | null = null;
  for (const step of steps) {
    for (const [key, value] of Object.entries(step.result ?? {})) {
      if (!key.endsWith("llm_usage")) continue;
      const usage = usageSchema.safeParse(value);
      if (!usage.success) continue;
      total ??= { requests: 0, prompt_tokens: 0, completion_tokens: 0, cost_usd: 0 };
      total.requests += usage.data.request_count;
      total.prompt_tokens += usage.data.prompt_tokens;
      total.completion_tokens += usage.data.completion_tokens;
      total.cost_usd += usage.data.cost_usd;
    }
  }
  return total;
}

function result<T>(steps: Step[], name: string, parser: z.ZodType<T>): T | null {
  const parsed = parser.safeParse(steps.find((step) => step.step_name === name)?.result);
  return parsed.success ? parsed.data : null;
}

export function jobFinished(task: FinishedTask, now: number): JobFinished {
  // A review re-runs only tagging, validation and fidelity; the earlier
  // steps' timings and model use belong to the upload's own event.
  const ran = task.kind === "process" ? PIPELINE_STEPS : RERUN_STEPS;
  const steps = task.steps.filter((step) => ran.includes(step.step_name));
  const reviewTasks: JobFinished["review_tasks"] = {};
  let visibleBlocking = 0;
  for (const reviewTask of task.reviewTasks) {
    if (reviewTask.blocking) visibleBlocking += 1;
    const count = reviewTasks[reviewTask.task_type] ?? { total: 0, blocking: 0 };
    count.total += 1;
    if (reviewTask.blocking) count.blocking += 1;
    reviewTasks[reviewTask.task_type] = count;
  }
  const fidelity = result(steps, "fidelity", fidelityResultSchema);
  const failed = task.status === "failed";
  return {
    event: JOB_FINISHED,
    job: task.jobId,
    kind: task.kind,
    attempt: task.attempt,
    status: task.status,
    failure: task.failure,
    failed_step: failed ? failedStep(task.stepsBefore.filter((step) => ran.includes(step.step_name))) : null,
    classification: task.classification ?? result(task.steps, "classify", classifyResultSchema)?.type ?? null,
    pages: task.pages,
    seconds: Math.round((now - task.since) / 1000),
    steps: Object.fromEntries(
      steps.map((step) => [step.step_name, { status: step.status, seconds: seconds(step.started_at, step.completed_at) }]),
    ),
    validation_compliant: task.validationCompliant,
    blocking_tasks: fidelity?.blocking_tasks ?? null,
    advisory_tasks: fidelity?.advisory_tasks ?? null,
    review_tasks: reviewTasks,
    hidden_blocking_tasks: fidelity === null ? null : Math.max(0, fidelity.blocking_tasks - visibleBlocking),
    held_by: [...new Set(task.checks.filter((check) => HOLDING_STATUSES.includes(check.status)).map((check) => check.check))],
    model_usage: modelUsage(steps),
  };
}
