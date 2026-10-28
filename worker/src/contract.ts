/**
 * Everything that crosses the Worker's boundaries, as parsers: the runner's
 * requests, the views it renders (v1's Pydantic responses), and people's
 * review requests. Field lists follow backend/app/schemas.py.
 */
import { z } from "zod";

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export const jsonValue: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([z.string(), z.number(), z.boolean(), z.null(), z.array(jsonValue), z.record(z.string(), jsonValue)]),
);
const jsonRecord = z.record(z.string(), jsonValue);

export const PIPELINE_STEPS = ["classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"];
export const RERUN_STEPS = ["tagging", "validation", "fidelity"];
export const OUTPUT_READY_STATUSES = ["complete", "manual_remediation"];
export const ACTIVE_STATUSES = ["queued", "processing"];

export const stepSchema = z.object({
  step_name: z.string(),
  status: z.string(),
  started_at: z.string().nullable(),
  completed_at: z.string().nullable(),
  error: z.string().nullable(),
  result: jsonRecord.nullable(),
});
export type Step = z.infer<typeof stepSchema>;
export const stepsSchema = z.array(stepSchema);

/** The runner's rendering of v1's JobResponse; the Worker owns the other fields. */
export const jobViewSchema = z.object({
  status: z.string(),
  classification: z.string().nullable(),
  ocr_language: z.string().nullable(),
  page_count: z.number().int().nullable(),
  error: z.string().nullable(),
  validation_compliant: z.boolean().nullable(),
  steps: stepsSchema,
});
export type JobView = z.infer<typeof jobViewSchema>;

export const claimRequestSchema = z.object({ runner: z.string().min(1).max(200) });

export const progressEventSchema = z.object({
  step: z.string().optional(),
  status: z.string().optional(),
  result: jsonRecord.optional(),
  message: z.string().optional(),
});
export type ProgressEvent = z.infer<typeof progressEventSchema>;

export const completeRequestSchema = z.union([
  z.object({ review_refused: z.string().max(2000), overrides_version: z.number().int().min(0) }),
  z.object({
    job: jobViewSchema,
    figures: z.array(z.number().int().min(0)),
    previews: z.array(z.number().int().min(1)),
    files: z.array(z.string()),
    has_state: z.boolean(),
    overrides_version: z.number().int().min(0),
  }),
]);
export type CompleteRequest = z.infer<typeof completeRequestSchema>;

export const failRequestSchema = z.object({ error: z.string().min(1).max(2000) });

export const overridesSchema = z.object({
  changes: z.record(z.string(), z.literal("kept")),
  tasks: z.record(z.string(), z.literal("resolved")),
});
export type Overrides = z.infer<typeof overridesSchema>;

export const appliedChangeSchema = z.object({
  id: z.number().int(),
  change_type: z.string(),
  title: z.string(),
  detail: z.string(),
  importance: z.string(),
  review_status: z.string(),
  reviewable: z.boolean(),
  metadata: jsonRecord,
  before: jsonRecord,
  after: jsonRecord,
});
export type AppliedChange = z.infer<typeof appliedChangeSchema>;
export const appliedChangesSchema = z.array(appliedChangeSchema);

export const reviewTaskSchema = z.object({
  id: z.number().int(),
  task_type: z.string(),
  title: z.string(),
  detail: z.string(),
  severity: z.string(),
  blocking: z.boolean(),
  status: z.string(),
  source: z.string(),
  metadata: jsonRecord,
});
export type ReviewTask = z.infer<typeof reviewTaskSchema>;
export const reviewTasksSchema = z.array(reviewTaskSchema);

export const batchPartsSchema = z.object({
  styles: z.string(),
  header: z.string(),
  footer: z.string(),
  row: z.string(),
  section: z.string(),
});
export type BatchParts = z.infer<typeof batchPartsSchema>;

export const reviseRequestSchema = z.object({ feedback: z.string().max(4000).nullable().optional() }).nullable();
export const editRequestSchema = z.object({ text: z.string().max(4000) });

export const reviewActionSchema = z.object({
  type: z.enum(["undo", "revise", "edit"]),
  change_id: z.number().int().min(1),
  feedback: z.string().optional(),
  text: z.string().optional(),
});
export type ReviewAction = z.infer<typeof reviewActionSchema>;

/** Files a runner may write for a revision. */
export function isRunnerFileName(name: string): boolean {
  return /^(?:views\/(?:review-tasks|applied-changes|figure-changes|report|batch-parts)\.json|report\.html|output\.pdf|state\.tar\.gz|figures\/\d{1,4}|previews\/\d{1,4})$/.test(
    name,
  );
}
