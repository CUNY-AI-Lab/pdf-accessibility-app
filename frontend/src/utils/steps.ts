import type { PipelineStep, StepName } from "../types";

const STEP_ORDER: StepName[] = [
  "classify",
  "ocr",
  "structure",
  "alt_text",
  "tagging",
  "validation",
  "fidelity",
];

/** Every pipeline step in order, pending until the job reports it. */
export function pipelineSteps(steps?: PipelineStep[]): PipelineStep[] {
  const reported = new Map((steps ?? []).map((step) => [step.step_name, step]));
  return STEP_ORDER.map(
    (step_name) => reported.get(step_name) ?? { step_name, status: "pending" },
  );
}
