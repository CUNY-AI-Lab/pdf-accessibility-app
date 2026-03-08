import type { ReviewTask } from "../types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EvidenceField = {
  key: string;
  label: string;
  placeholder: string;
};

export type FontReviewTarget = {
  rule_id?: string;
  page?: number;
  font?: string;
  count?: number;
  content_stream_index?: number;
  operator_index?: number;
  sample_context?: string;
  decoded_text?: string;
  before_text?: string;
  after_text?: string;
  nearby_text?: string;
};

export type LlmSuggestion = {
  summary?: string;
  confidence?: string;
  suggested_action?: string;
  reason?: string;
  generated_at?: string;
  model?: string;
  reviewer_checklist?: string[];
  review_focus?: Array<{
    page?: number;
    font?: string;
    operator_index?: number;
    rule_id?: string;
    visible_text_hypothesis?: string;
    is_likely_decorative?: boolean;
    recommended_reviewer_action?: string;
  }>;
  actualtext_candidates?: Array<{
    page?: number;
    operator_index?: number;
    font?: string;
    proposed_actualtext?: string;
    confidence?: string;
    reason?: string;
  }>;
};

export type EditableStructureElement = Record<string, unknown> & {
  review_id: string;
  page?: number;
  type?: string;
  text?: string;
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const TASK_EVIDENCE_FIELDS: Record<string, EvidenceField[]> = {
  reading_order: [
    {
      key: "verification_method",
      label: "Verification method",
      placeholder: "NVDA, exported text audit, or Acrobat reading order check",
    },
    {
      key: "pages_checked",
      label: "Pages checked",
      placeholder: "Pages 1-5 and all pages with sidebars or callouts",
    },
  ],
  font_text_fidelity: [
    {
      key: "assistive_tech",
      label: "Assistive technology",
      placeholder: "NVDA, VoiceOver, copy/paste audit, or text export",
    },
    {
      key: "sample_scope",
      label: "Sample scope",
      placeholder: "Cover page, formula pages, and a random spot check",
    },
  ],
  table_semantics: [
    {
      key: "tables_checked",
      label: "Tables checked",
      placeholder: "Tables on pages 2, 7, and 11",
    },
    {
      key: "verification_method",
      label: "Verification method",
      placeholder: "Screen-reader table navigation and tags inspection",
    },
  ],
  content_fidelity: [
    {
      key: "comparison_method",
      label: "Comparison method",
      placeholder: "Visible-vs-extracted text comparison or OCR spot check",
    },
    {
      key: "pages_checked",
      label: "Pages checked",
      placeholder: "First 3 pages and all pages with formulas or figures",
    },
  ],
  alt_text: [
    {
      key: "figures_checked",
      label: "Figures checked",
      placeholder: "Figures 1-4 and every chart or diagram",
    },
  ],
};

export const LLM_SUGGESTION_TASK_TYPES = new Set(["font_text_fidelity", "reading_order"]);

export const STRUCTURE_TYPE_OPTIONS = [
  { value: "paragraph", label: "Paragraph" },
  { value: "heading", label: "Heading" },
  { value: "list_item", label: "List Item" },
  { value: "code", label: "Code" },
  { value: "formula", label: "Formula" },
  { value: "artifact", label: "Artifact" },
] as const;

// ---------------------------------------------------------------------------
// Pure helper functions
// ---------------------------------------------------------------------------

export function evidenceFieldsForTask(taskType: string): EvidenceField[] {
  return TASK_EVIDENCE_FIELDS[taskType] ?? [];
}

export function existingResolutionNote(task: ReviewTask): string {
  return typeof task.metadata?.resolution_note === "string"
    ? task.metadata.resolution_note
    : "";
}

export function existingEvidenceForTask(task: ReviewTask): Record<string, string> {
  const rawEvidence = task.metadata?.evidence;
  if (!rawEvidence || typeof rawEvidence !== "object" || Array.isArray(rawEvidence)) {
    return {};
  }

  return Object.fromEntries(
    Object.entries(rawEvidence as Record<string, unknown>)
      .filter(([key]) => key.trim().length > 0)
      .map(([key, value]) => [key, typeof value === "string" ? value : String(value ?? "")]),
  );
}

export function metadataEntriesForTask(task: ReviewTask): Array<[string, string]> {
  return Object.entries(task.metadata ?? {})
    .filter(
      ([key]) => key !== "resolution_note"
        && key !== "evidence"
        && key !== "llm_suggestion"
        && key !== "manual_actualtext_attempts"
        && key !== "font_rule_ids"
        && key !== "pages_to_check"
        && key !== "fonts_to_check"
        && key !== "font_review_targets",
    )
    .map(([key, value]) => {
      if (value && typeof value === "object") {
        return [key, JSON.stringify(value)];
      }
      return [key, String(value)];
    });
}

export function manualActualTextAttempts(task: ReviewTask): Array<{
  page_number?: number;
  operator_index?: number;
  actual_text?: string;
  applied_at?: string;
  mode?: string;
}> {
  const value = task.metadata?.manual_actualtext_attempts;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is {
      page_number?: number;
      operator_index?: number;
      actual_text?: string;
      applied_at?: string;
      mode?: string;
    } => !!item && typeof item === "object" && !Array.isArray(item),
  );
}

export function manualFontMappingAttempts(task: ReviewTask): Array<{
  page_number?: number;
  operator_index?: number;
  unicode_text?: string;
  font_base_name?: string;
  font_code_hex?: string;
  applied_at?: string;
}> {
  const value = task.metadata?.manual_font_mapping_attempts;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is {
      page_number?: number;
      operator_index?: number;
      unicode_text?: string;
      font_base_name?: string;
      font_code_hex?: string;
      applied_at?: string;
    } => !!item && typeof item === "object" && !Array.isArray(item),
  );
}

export function stringListMetadata(task: ReviewTask, key: string): string[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "string" ? item.trim() : String(item ?? "").trim()))
    .filter((item) => item.length > 0);
}

export function numberListMetadata(task: ReviewTask, key: string): number[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "number" ? item : Number(item)))
    .filter((item) => Number.isFinite(item) && item > 0);
}

export function fontReviewTargets(task: ReviewTask): FontReviewTarget[] {
  const value = task.metadata?.font_review_targets;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      rule_id: typeof item.rule_id === "string" ? item.rule_id : undefined,
      page: typeof item.page === "number" ? item.page : undefined,
      font: typeof item.font === "string" ? item.font : undefined,
      count: typeof item.count === "number" ? item.count : undefined,
      content_stream_index: typeof item.content_stream_index === "number" ? item.content_stream_index : undefined,
      operator_index: typeof item.operator_index === "number" ? item.operator_index : undefined,
      sample_context: typeof item.sample_context === "string" ? item.sample_context : undefined,
      decoded_text: typeof item.decoded_text === "string" ? item.decoded_text : undefined,
      before_text: typeof item.before_text === "string" ? item.before_text : undefined,
      after_text: typeof item.after_text === "string" ? item.after_text : undefined,
      nearby_text: typeof item.nearby_text === "string" ? item.nearby_text : undefined,
    }));
}

export function llmSuggestionForTask(task: ReviewTask): LlmSuggestion | null {
  const value = task.metadata?.llm_suggestion;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as LlmSuggestion;
}

export function pagePreviewUrl(jobId: string, pageNumber: number): string {
  return `/api/jobs/${jobId}/pages/${pageNumber}/preview`;
}

export function fontTargetPreviewUrl(
  jobId: string,
  taskId: number,
  target: FontReviewTarget,
): string | null {
  if (typeof target.page !== "number" || typeof target.operator_index !== "number") {
    return null;
  }
  const params = new URLSearchParams({
    page_number: String(target.page),
    operator_index: String(target.operator_index),
  });
  return `/api/jobs/${jobId}/review-tasks/${taskId}/font-target-preview?${params.toString()}`;
}

export function actualTextCandidateForTarget(
  llmSuggestion: LlmSuggestion | null,
  target: FontReviewTarget,
) {
  const candidates = llmSuggestion?.actualtext_candidates;
  if (!Array.isArray(candidates)) {
    return null;
  }
  return (
    candidates.find((candidate) => {
      const candidatePage = typeof candidate.page === "number" ? candidate.page : null;
      const candidateOperator =
        typeof candidate.operator_index === "number" ? candidate.operator_index : null;
      const candidateFont =
        typeof candidate.font === "string" ? candidate.font.trim() : null;
      const targetFont = typeof target.font === "string" ? target.font.trim() : null;

      return candidatePage === target.page
        && candidateOperator === target.operator_index
        && (candidateFont === null || targetFont === null || candidateFont === targetFont);
    }) ?? null
  );
}

export function applicableActualTextCandidates(
  llmSuggestion: LlmSuggestion | null,
  targets: FontReviewTarget[],
): Array<{
  page: number;
  operator_index: number;
  font?: string;
  proposed_actualtext: string;
  confidence?: string;
  reason?: string;
}> {
  const candidates = llmSuggestion?.actualtext_candidates;
  if (!Array.isArray(candidates)) {
    return [];
  }

  return candidates.filter((candidate) => {
    if (
      typeof candidate.page !== "number"
      || typeof candidate.operator_index !== "number"
      || typeof candidate.proposed_actualtext !== "string"
      || candidate.proposed_actualtext.trim().length === 0
    ) {
      return false;
    }
    return targets.some((target) => (
      target.page === candidate.page
      && target.operator_index === candidate.operator_index
      && (
        typeof candidate.font !== "string"
        || typeof target.font !== "string"
        || candidate.font.trim() === target.font.trim()
      )
    ));
  }) as Array<{
    page: number;
    operator_index: number;
    font?: string;
    proposed_actualtext: string;
    confidence?: string;
    reason?: string;
  }>;
}

export function previewPagesForTask(task: ReviewTask, llmSuggestion: LlmSuggestion | null): number[] {
  const pages = new Set<number>();

  for (const page of numberListMetadata(task, "pages_to_check")) {
    if (page > 0) {
      pages.add(page);
    }
  }

  for (const target of fontReviewTargets(task)) {
    if (typeof target.page === "number" && target.page > 0) {
      pages.add(target.page);
    }
  }

  for (const item of llmSuggestion?.review_focus ?? []) {
    if (typeof item.page === "number" && item.page > 0) {
      pages.add(item.page);
    }
  }

  return Array.from(pages).sort((a, b) => a - b).slice(0, 3);
}

export function ensureEditableStructure(
  structure: Record<string, unknown> | undefined,
): Record<string, unknown> | null {
  if (!structure || typeof structure !== "object") {
    return null;
  }

  const rawElements = Array.isArray(structure.elements) ? structure.elements : [];
  const elements = rawElements
    .filter((element): element is Record<string, unknown> => !!element && typeof element === "object" && !Array.isArray(element))
    .map((element, index) => ({
      ...element,
      review_id:
        typeof element.review_id === "string" && element.review_id.trim().length > 0
          ? element.review_id
          : `review-${index}`,
    }));

  return {
    ...structure,
    elements,
  };
}

export function structurePages(structure: Record<string, unknown> | null): number[] {
  const rawElements = Array.isArray(structure?.elements) ? structure.elements : [];
  const pages = new Set<number>();
  for (const element of rawElements) {
    if (!element || typeof element !== "object" || Array.isArray(element)) {
      continue;
    }
    const page = typeof element.page === "number" ? element.page + 1 : null;
    if (page && page > 0) {
      pages.add(page);
    }
  }
  return Array.from(pages).sort((a, b) => a - b);
}

export function structureElementsForPage(
  structure: Record<string, unknown> | null,
  pageNumber: number,
): Array<{ element: EditableStructureElement; index: number }> {
  const rawElements = Array.isArray(structure?.elements) ? structure.elements : [];
  return rawElements
    .map((element, index) => ({ element, index }))
    .filter(
      (
        entry,
      ): entry is { element: EditableStructureElement; index: number } =>
        !!entry.element
        && typeof entry.element === "object"
        && !Array.isArray(entry.element)
        && typeof entry.element.review_id === "string"
        && (typeof entry.element.page === "number" ? entry.element.page + 1 : null) === pageNumber,
    );
}

export function structureTypeLabel(type: string | undefined): string {
  const option = STRUCTURE_TYPE_OPTIONS.find((entry) => entry.value === type);
  return option?.label ?? String(type ?? "paragraph");
}

export function guidanceForTask(taskType: string): string[] {
  if (taskType === "reading_order") {
    return [
      "Read the document with a screen reader or exported text view.",
      "Check that headings, paragraphs, lists, and sidebars follow the intended order.",
    ];
  }
  if (taskType === "font_text_fidelity") {
    return [
      "Compare visible text against what copy/paste or a screen reader exposes.",
      "Pay attention to symbols, ligatures, math, and unusual fonts.",
    ];
  }
  if (taskType === "table_semantics") {
    return [
      "Verify header cells, spans, and reading order row by row.",
      "Confirm that assistive technology can identify the headers for each data cell.",
    ];
  }
  if (taskType === "content_fidelity") {
    return [
      "Check for missing text, duplicated text, or OCR drift.",
      "Compare the first pages and any pages with formulas or figures.",
    ];
  }
  if (taskType === "alt_text") {
    return [
      "Confirm the description matches the figure's purpose in context.",
      "Reject generic or hallucinated descriptions.",
    ];
  }
  return [
    "Review this issue directly in the PDF and with assistive technology if needed.",
  ];
}

export function actualTextKeyForTarget(taskId: number, target: FontReviewTarget): string {
  return `${taskId}:${target.page ?? "page"}:${target.operator_index ?? "op"}`;
}
