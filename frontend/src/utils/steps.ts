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

export type DisplayPipelineStep = Omit<PipelineStep, "step_name"> & { step_name: StepName };

/** Every pipeline step in order, pending until the job reports it. */
export function pipelineSteps(steps?: PipelineStep[]): DisplayPipelineStep[] {
  const reported = new Map((steps ?? []).map((step) => [step.step_name, step]));
  return STEP_ORDER.map(
    (step_name) => {
      const step = reported.get(step_name);
      return step ? { ...step, step_name } : { step_name, status: "pending" };
    },
  );
}
