import { JOB_STATUSES, type Job, type JobStatus } from "../types";
export { JOB_STATUSES, TERMINAL_JOB_STATUSES } from "../types";
import { pipelineSteps } from "../utils/steps";

// Projection vocabulary; the Worker owns job transitions, never the browser.
export function knownJobStatus(status: string): JobStatus | "unknown" {
  return JOB_STATUSES.find((value) => value === status) ?? "unknown";
}
export function outputReady(job?: Job): boolean {
  return job?.status === "complete" || job?.status === "manual_remediation";
}
export function jobRevision(job: Job): string {
  return `${job.id}:${job.updated_at}:${job.status}`;
}
export type JobView =
  | { status: "loading" }
  | { status: "unavailable"; reason: "missing" | "request" }
  | { status: "active" | "output_ready" | "failed" | "unknown"; job: Job };
export function projectJobView(job: Job | undefined, loading: boolean, error: Error | null): JobView {
  if (error) return { status: "unavailable", reason: "status" in error && error.status === 404 ? "missing" : "request" };
  if (loading) return { status: "loading" };
  if (!job) return { status: "unavailable", reason: "missing" };
  const status = knownJobStatus(job.status);
  if (status === "queued" || status === "processing") return { status: "active", job };
  if (outputReady(job)) return { status: "output_ready", job };
  return { status: status === "failed" ? "failed" : "unknown", job };
}
export function jobPollInterval(job?: Job): number {
  return job?.status === "queued" || job?.status === "processing" ? 3000 : 30000;
}
export function jobProgress(job: Job) {
  const steps = pipelineSteps(job.steps);
  const completed = steps.filter((step) => step.status === "complete" || step.status === "skipped").length;
  return { completed, total: steps.length, percent: completed / steps.length * 100 };
}
export function downloadCapabilities(job?: Job) {
  const ready = outputReady(job);
  // Legacy Worker responses expose only has_html. PDF/report links remain
  // inferred from output readiness; the file endpoint checks actual presence.
  return { pdf: ready, report: ready, html: ready && job?.has_html === true };
}

export type ReviewContext<T> =
  | { status: "not_applicable" }
  | { status: "loading"; revision: string }
  | { status: "unavailable"; revision: string; error: Error }
  | { status: "ready"; revision: string; data: T };
export function projectReviewContext<T>(job: Job | undefined, query: { data?: { revision: string; data: T }; error: Error | null; isFetching: boolean }): ReviewContext<T> {
  if (!job || !outputReady(job)) return { status: "not_applicable" };
  const revision = jobRevision(job);
  if (query.error) return { status: "unavailable", revision, error: query.error };
  if (!query.data || query.data.revision !== revision) return { status: "loading", revision };
  return { status: "ready", revision, data: query.data.data };
}

/** Bracket independently served views with the deployed Worker's job identity. */
export async function loadReviewSnapshot<T>(job: Job, readJob: () => Promise<Job>, readViews: () => Promise<T>) {
  const revision = jobRevision(job);
  const before = await readJob();
  if (!outputReady(before) || jobRevision(before) !== revision) throw new Error("Output changed before loading review details");
  const data = await readViews();
  const after = await readJob();
  if (!outputReady(after) || jobRevision(after) !== revision) throw new Error("Output changed while loading review details");
  return { revision, data };
}

export function complianceOutcome(status: string, validated: boolean, context: ReviewContext<unknown>["status"]): "passed" | "needs_fixes" | "unverified" {
  if (status === "manual_remediation") return "needs_fixes";
  return status === "complete" && validated && context === "ready" ? "passed" : "unverified";
}
