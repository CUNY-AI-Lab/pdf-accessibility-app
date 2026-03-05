export type JobStatus =
  | "queued"
  | "processing"
  | "awaiting_review"
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
}

export interface ValidationReport {
  compliant: boolean;
  violations: ValidationViolation[];
  summary: Record<string, number>;
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
