export type JobStatus =
  | "queued"
  | "processing"
  | "awaiting_review"
  | "needs_manual_review"
  | "complete"
  | "failed";

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
  | "validation";

export type AltTextStatus = "pending_review" | "approved" | "rejected";

export interface PipelineStep {
  step_name: StepName;
  status: StepStatus;
  started_at?: string;
  completed_at?: string;
  error?: string;
  result?: Record<string, unknown>;
}

export interface Job {
  id: string;
  filename: string;
  original_filename: string;
  status: JobStatus;
  classification?: "scanned" | "digital" | "mixed";
  page_count?: number;
  file_size_bytes?: number;
  error?: string;
  created_at: string;
  updated_at: string;
  steps: PipelineStep[];
}

export interface AltText {
  id: number;
  figure_index: number;
  image_url: string;
  generated_text?: string;
  edited_text?: string;
  status: AltTextStatus;
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
  tagging?: Record<string, unknown>;
  claims?: Record<string, unknown>;
}

export interface StructureElement {
  type: string;
  text?: string;
  level?: number;
  children?: StructureElement[];
}

export interface ProgressEvent {
  step: StepName | "review" | "error";
  status: string;
  timestamp?: string;
  message?: string;
  result?: Record<string, unknown>;
}
