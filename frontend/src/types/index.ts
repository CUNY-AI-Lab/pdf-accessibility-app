export const JOB_STATUSES = ["queued", "processing", "complete", "manual_remediation", "failed"] as const;
export const TERMINAL_JOB_STATUSES = ["complete", "manual_remediation", "failed"] as const;
export type TerminalJobStatus = (typeof TERMINAL_JOB_STATUSES)[number];
export type JobStatus = (typeof JOB_STATUSES)[number];

export type StepStatus =
  | "pending"
  | "running"
  | "complete"
  | "skipped"
  | "failed";

export type StepName =
  | "classify"
  | "ocr"
  | "structure"
  | "alt_text"
  | "tagging"
  | "validation"
  | "fidelity";

export interface PipelineStep {
  step_name: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  error?: string;
  result?: Record<string, unknown>;
}

export interface Job {
  id: string;
  filename: string;
  original_filename: string;
  status: string;
  classification?: "scanned" | "digital" | "mixed" | "ocr_scan";
  ocr_language?: string;
  page_count?: number;
  file_size_bytes?: number;
  error?: string;
  /** Why the last review action could not be applied, when it could not. */
  notice?: string | null;
  validation_compliant?: boolean | null;
  /** The v2 Worker's current revision includes an accessible HTML document. */
  has_html?: boolean;
  created_at: string;
  updated_at: string;
  steps: PipelineStep[];
}

export interface ReviewTask {
  id: number;
  task_type: string;
  title: string;
  detail: string;
  severity: "high" | "medium" | "low";
  blocking: boolean;
  status: string;
  source: string;
  metadata: Record<string, unknown>;
}

export interface AppliedChange {
  id: number;
  change_type: string;
  title: string;
  detail: string;
  importance: "high" | "medium" | "low";
  review_status: string;
  reviewable: boolean;
  metadata: Record<string, unknown>;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
}

export interface ValidationViolation {
  rule_id: string;
  description: string;
  severity: "error" | "warning";
  location?: string;
  count: number;
  category?: string;
  fix_hint?: string;
  remediation_status?: "needs_remediation" | "auto_remediated" | "manual_remediated";
}

export interface ValidationChange {
  rule_id: string;
  description: string;
  severity: "error" | "warning";
  location?: string;
  category?: string;
  fix_hint?: string;
  baseline_count: number;
  post_count: number;
  remediation_status: "needs_remediation" | "auto_remediated" | "manual_remediated";
}

export interface ValidationReport {
  compliant: boolean;
  profile?: string;
  standard?: string;
  validator?: string;
  generated_at?: string;
  baseline?: Record<string, unknown>;
  violations: ValidationViolation[];
  changes?: ValidationChange[];
  summary: Record<string, number>;
  remediation?: Record<string, unknown>;
  fidelity?: Record<string, unknown>;
  tagging?: Record<string, unknown>;
  claims?: Record<string, unknown>;
}

