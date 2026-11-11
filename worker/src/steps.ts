import { PIPELINE_STEPS, RERUN_STEPS, type Step, type ProgressEvent } from "./contract";
export type StepStatus = "pending" | "running" | "complete" | "skipped" | "failed";
const TRANSITIONS = {
  pending: ["pending", "running", "complete", "skipped", "failed"],
  running: ["running", "complete", "skipped", "failed"],
  complete: ["complete"],
  skipped: ["skipped"],
  failed: ["failed"],
} satisfies Record<StepStatus, readonly StepStatus[]>;

export function guardStepTransition(current: string, target: string): void {
  if (!Object.entries(TRANSITIONS).some(([state, targets]) => state === current && targets.some((value) => value === target))) {
    throw new Error(`Illegal step transition: ${current} -> ${target}`);
  }
}

export function validateSteps(steps: Step[]): void {
  if (steps.length !== PIPELINE_STEPS.length || new Set(steps.map((step) => step.step_name)).size !== PIPELINE_STEPS.length) {
    throw new Error("Completion requires each pipeline step exactly once");
  }
  for (const step of steps) {
    if (!PIPELINE_STEPS.includes(step.step_name)) throw new Error(`Unknown pipeline step: ${step.step_name}`);
    guardStepTransition(step.status, step.status);
  }
}

export function resetSteps(steps: Step[], review: boolean): Step[] {
  return steps.map((step) => review && !RERUN_STEPS.includes(step.step_name) ? step : {
    ...step, status: "pending", started_at: null, completed_at: null, error: null, result: null,
  });
}

export function applyStepProgress(steps: Step[], event: ProgressEvent, now: number): Step[] {
  if (event.step === undefined && event.status === undefined) return steps;
  // Deployed runners send the final job outcome under "review". It is not a pipeline step.
  if (event.step === "review" && ["complete", "manual_remediation", "failed"].includes(event.status ?? "")) return steps;
  const target = steps.find((step) => step.step_name === event.step);
  if (target === undefined || event.status === undefined) throw new Error("Unknown or incomplete pipeline progress event");
  guardStepTransition(target.status, event.status);
  return steps.map((step) => step !== target ? step : {
    ...step,
    status: event.status ?? step.status,
    started_at: event.status === "running" ? (step.started_at ?? new Date(now).toISOString()) : step.started_at,
    completed_at: ["complete", "skipped", "failed"].includes(event.status ?? "") ? new Date(now).toISOString() : null,
    error: event.status === "failed" ? (event.message ?? step.error) : null,
    result: event.result ?? (event.status === "running" ? null : step.result),
  });
}
