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

export type TableReviewTarget = {
  table_review_id?: string;
  page?: number;
  num_rows?: number;
  num_cols?: number;
  text_excerpt?: string;
  header_rows?: number[];
  row_header_columns?: number[];
  bbox?: {
    l?: number;
    t?: number;
    r?: number;
    b?: number;
  };
  risk_score?: number;
  risk_reasons?: string[];
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
  proposed_page_orders?: Array<{
    page?: number;
    ordered_review_ids?: string[];
    reason?: string;
  }>;
  proposed_element_updates?: Array<{
    page?: number;
    review_id?: string;
    new_type?: string;
    new_level?: number;
    reason?: string;
  }>;
  proposed_table_updates?: Array<{
    page?: number;
    table_review_id?: string;
    header_rows?: number[];
    row_header_columns?: number[];
    reason?: string;
  }>;
  readable_text_hints?: Array<{
    page?: number;
    review_id?: string;
    extracted_text?: string;
    readable_text_hint?: string;
    issue_type?: string;
    confidence?: string;
    should_block_accessibility?: boolean;
    reason?: string;
  }>;
  document_overlay?: {
    provenance?: string;
    pages?: Array<{
      page_number?: number;
      blocks?: Array<{
        review_id?: string;
        role?: string;
        text?: string;
        level?: number;
        provenance?: string;
        confidence?: number;
      }>;
      tables?: Array<{
        table_review_id?: string;
        header_rows?: number[];
        row_header_columns?: number[];
        provenance?: string;
        confidence?: number;
      }>;
    }>;
  };
};

export type EditableStructureElement = Record<string, unknown> & {
  review_id: string;
  page?: number;
  type?: string;
  text?: string;
};

export type ReadingOrderPageOrder = {
  page: number;
  ordered_review_ids: string[];
  reason?: string;
};

export type ReadingOrderElementUpdate = {
  page?: number;
  review_id: string;
  new_type: string;
  new_level?: number;
  reason?: string;
};

export type TableHeaderUpdate = {
  page?: number;
  table_review_id: string;
  header_rows: number[];
  row_header_columns: number[];
  reason?: string;
};

export type ReadingOrderTextHint = {
  page?: number;
  review_id: string;
  extracted_text?: string;
  readable_text_hint?: string;
  issue_type?: string;
  confidence?: string;
  should_block_accessibility?: boolean;
  reason?: string;
};

export type DocumentOverlayBlock = {
  review_id: string;
  role?: string;
  text?: string;
  level?: number;
  semantic_text_hint?: string;
  semantic_issue_type?: string;
  semantic_blocking?: boolean;
  provenance?: string;
  confidence?: number;
};

export type DocumentOverlayTable = {
  table_review_id: string;
  header_rows: number[];
  row_header_columns: number[];
  provenance?: string;
  confidence?: number;
};

export type DocumentOverlayPage = {
  page_number: number;
  blocks: DocumentOverlayBlock[];
  tables: DocumentOverlayTable[];
};

export type DocumentOverlay = {
  provenance?: string;
  pages: DocumentOverlayPage[];
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

export const LLM_SUGGESTION_TASK_TYPES = new Set(["font_text_fidelity", "reading_order", "table_semantics"]);

export const STRUCTURE_TYPE_OPTIONS = [
  { value: "paragraph", label: "Paragraph" },
  { value: "heading", label: "Heading" },
  { value: "list_item", label: "List Item" },
  { value: "code", label: "Code" },
  { value: "formula", label: "Formula" },
  { value: "artifact", label: "Hide (Decorative)" },
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

export function tableReviewTargets(task: ReviewTask): TableReviewTarget[] {
  const value = task.metadata?.table_review_targets;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      table_review_id: typeof item.table_review_id === "string" ? item.table_review_id : undefined,
      page: typeof item.page === "number" ? item.page : undefined,
      num_rows: typeof item.num_rows === "number" ? item.num_rows : undefined,
      num_cols: typeof item.num_cols === "number" ? item.num_cols : undefined,
      text_excerpt: typeof item.text_excerpt === "string" ? item.text_excerpt : undefined,
      header_rows: Array.isArray(item.header_rows)
        ? item.header_rows
            .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
            .filter((entry) => Number.isFinite(entry) && entry >= 0)
        : undefined,
      row_header_columns: Array.isArray(item.row_header_columns)
        ? item.row_header_columns
            .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
            .filter((entry) => Number.isFinite(entry) && entry >= 0)
        : undefined,
      bbox: item.bbox && typeof item.bbox === "object" && !Array.isArray(item.bbox)
        ? item.bbox as { l?: number; t?: number; r?: number; b?: number }
        : undefined,
      risk_score: typeof item.risk_score === "number" ? item.risk_score : undefined,
      risk_reasons: Array.isArray(item.risk_reasons)
        ? item.risk_reasons
            .map((entry) => (typeof entry === "string" ? entry.trim() : String(entry ?? "").trim()))
            .filter((entry) => entry.length > 0)
        : undefined,
    }));
}

export function llmSuggestionForTask(task: ReviewTask): LlmSuggestion | null {
  const value = task.metadata?.llm_suggestion;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as LlmSuggestion;
}

export function readingOrderPageOrders(llmSuggestion: LlmSuggestion | null): ReadingOrderPageOrder[] {
  const value = llmSuggestion?.proposed_page_orders;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      page: typeof item.page === "number" ? item.page : NaN,
      ordered_review_ids: Array.isArray(item.ordered_review_ids)
        ? item.ordered_review_ids
            .map((entry) => (typeof entry === "string" ? entry.trim() : ""))
            .filter((entry) => entry.length > 0)
        : [],
      reason: typeof item.reason === "string" ? item.reason : undefined,
    }))
    .filter((item) => Number.isFinite(item.page) && item.page > 0 && item.ordered_review_ids.length > 0);
}

export function readingOrderElementUpdates(llmSuggestion: LlmSuggestion | null): ReadingOrderElementUpdate[] {
  const value = llmSuggestion?.proposed_element_updates;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      page: typeof item.page === "number" ? item.page : undefined,
      review_id: typeof item.review_id === "string" ? item.review_id.trim() : "",
      new_type: typeof item.new_type === "string" ? item.new_type.trim() : "",
      new_level: typeof item.new_level === "number" ? item.new_level : undefined,
      reason: typeof item.reason === "string" ? item.reason : undefined,
    }))
    .filter((item) => item.review_id.length > 0 && item.new_type.length > 0);
}

export function tableHeaderUpdates(llmSuggestion: LlmSuggestion | null): TableHeaderUpdate[] {
  const value = llmSuggestion?.proposed_table_updates;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      page: typeof item.page === "number" ? item.page : undefined,
      table_review_id: typeof item.table_review_id === "string" ? item.table_review_id.trim() : "",
      header_rows: Array.isArray(item.header_rows)
        ? item.header_rows
            .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
            .filter((entry) => Number.isFinite(entry) && entry >= 0)
        : [],
      row_header_columns: Array.isArray(item.row_header_columns)
        ? item.row_header_columns
            .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
            .filter((entry) => Number.isFinite(entry) && entry >= 0)
        : [],
      reason: typeof item.reason === "string" ? item.reason : undefined,
    }))
    .filter((item) => item.table_review_id.length > 0);
}

export function tableHeaderUpdateForTarget(
  llmSuggestion: LlmSuggestion | null,
  tableReviewId: string | undefined,
): TableHeaderUpdate | null {
  if (!tableReviewId) {
    return null;
  }
  return tableHeaderUpdates(llmSuggestion).find((item) => item.table_review_id === tableReviewId) ?? null;
}

export function readingOrderTextHints(llmSuggestion: LlmSuggestion | null): ReadingOrderTextHint[] {
  const value = llmSuggestion?.readable_text_hints;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      page: typeof item.page === "number" ? item.page : undefined,
      review_id: typeof item.review_id === "string" ? item.review_id.trim() : "",
      extracted_text: typeof item.extracted_text === "string" ? item.extracted_text : undefined,
      readable_text_hint: typeof item.readable_text_hint === "string" ? item.readable_text_hint : undefined,
      issue_type: typeof item.issue_type === "string" ? item.issue_type : undefined,
      confidence: typeof item.confidence === "string" ? item.confidence : undefined,
      should_block_accessibility: typeof item.should_block_accessibility === "boolean"
        ? item.should_block_accessibility
        : undefined,
      reason: typeof item.reason === "string" ? item.reason : undefined,
    }))
    .filter((item) => item.review_id.length > 0 && typeof item.readable_text_hint === "string" && item.readable_text_hint.trim().length > 0);
}

export function documentOverlayForSuggestion(llmSuggestion: LlmSuggestion | null): DocumentOverlay | null {
  const value = llmSuggestion?.document_overlay;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  const rawPages = Array.isArray(value.pages) ? value.pages : [];
  const pages = rawPages
    .filter((page): page is Record<string, unknown> => !!page && typeof page === "object" && !Array.isArray(page))
    .map((page) => {
      const blocks = Array.isArray(page.blocks)
        ? page.blocks
            .filter((block): block is Record<string, unknown> => !!block && typeof block === "object" && !Array.isArray(block))
            .map((block) => ({
              review_id: typeof block.review_id === "string" ? block.review_id.trim() : "",
              role: typeof block.role === "string" ? block.role : undefined,
              text: typeof block.text === "string" ? block.text : undefined,
              level: typeof block.level === "number" ? block.level : undefined,
              semantic_text_hint: typeof block.semantic_text_hint === "string" ? block.semantic_text_hint : undefined,
              semantic_issue_type: typeof block.semantic_issue_type === "string" ? block.semantic_issue_type : undefined,
              semantic_blocking: typeof block.semantic_blocking === "boolean" ? block.semantic_blocking : undefined,
              provenance: typeof block.provenance === "string" ? block.provenance : undefined,
              confidence: typeof block.confidence === "number" ? block.confidence : undefined,
            }))
            .filter((block) => block.review_id.length > 0)
        : [];

      const tables = Array.isArray(page.tables)
        ? page.tables
            .filter((table): table is Record<string, unknown> => !!table && typeof table === "object" && !Array.isArray(table))
            .map((table) => ({
              table_review_id: typeof table.table_review_id === "string" ? table.table_review_id.trim() : "",
              header_rows: Array.isArray(table.header_rows)
                ? table.header_rows
                    .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
                    .filter((entry) => Number.isFinite(entry) && entry >= 0)
                : [],
              row_header_columns: Array.isArray(table.row_header_columns)
                ? table.row_header_columns
                    .map((entry) => (typeof entry === "number" ? entry : Number(entry)))
                    .filter((entry) => Number.isFinite(entry) && entry >= 0)
                : [],
              provenance: typeof table.provenance === "string" ? table.provenance : undefined,
              confidence: typeof table.confidence === "number" ? table.confidence : undefined,
            }))
            .filter((table) => table.table_review_id.length > 0)
        : [];

      return {
        page_number: typeof page.page_number === "number" ? page.page_number : NaN,
        blocks,
        tables,
      };
    })
    .filter((page) => Number.isFinite(page.page_number) && page.page_number > 0);

  return {
    provenance: typeof value.provenance === "string" ? value.provenance : undefined,
    pages,
  };
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

export function tableTargetPreviewUrl(
  jobId: string,
  taskId: number,
  target: TableReviewTarget,
): string | null {
  if (typeof target.table_review_id !== "string" || target.table_review_id.trim().length === 0) {
    return null;
  }
  const params = new URLSearchParams({
    table_review_id: target.table_review_id,
  });
  return `/api/jobs/${jobId}/review-tasks/${taskId}/table-target-preview?${params.toString()}`;
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

  for (const target of tableReviewTargets(task)) {
    if (typeof target.page === "number" && target.page > 0) {
      pages.add(target.page);
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

function _applyReadingOrderElementUpdate(
  element: EditableStructureElement,
  update: ReadingOrderElementUpdate,
): EditableStructureElement {
  const nextType = update.new_type;
  if (!nextType) {
    return element;
  }

  if (nextType === "artifact") {
    return {
      ...element,
      _manual_original_type:
        typeof element.type === "string" && element.type.length > 0
          ? element.type
          : "paragraph",
      type: "artifact",
    };
  }

  const nextElement: EditableStructureElement = {
    ...element,
    type: nextType,
  };
  delete nextElement._manual_original_type;

  if (nextType === "heading") {
    nextElement.level =
      typeof update.new_level === "number" && update.new_level >= 1 && update.new_level <= 6
        ? update.new_level
        : (typeof element.level === "number" && element.level >= 1 && element.level <= 6 ? element.level : 1);
  } else {
    delete nextElement.level;
  }

  return nextElement;
}

export function canApplyReadingOrderSuggestion(
  structure: Record<string, unknown> | null,
  llmSuggestion: LlmSuggestion | null,
): boolean {
  if (!structure || !Array.isArray(structure.elements)) {
    return false;
  }
  const pageOrders = readingOrderPageOrders(llmSuggestion);
  const elementUpdates = readingOrderElementUpdates(llmSuggestion);
  if (pageOrders.length === 0 && elementUpdates.length === 0) {
    return false;
  }

  for (const pageOrder of pageOrders) {
    const pageEntries = structureElementsForPage(structure, pageOrder.page);
    const currentIds = pageEntries.map(({ element }) => element.review_id);
    if (currentIds.length === 0 || currentIds.length !== pageOrder.ordered_review_ids.length) {
      return false;
    }
    const currentSet = new Set(currentIds);
    if (new Set(pageOrder.ordered_review_ids).size !== pageOrder.ordered_review_ids.length) {
      return false;
    }
    if (pageOrder.ordered_review_ids.some((reviewId) => !currentSet.has(reviewId))) {
      return false;
    }
  }

  const validTypes = new Set<string>(STRUCTURE_TYPE_OPTIONS.map((option) => option.value));
  const elements = structure.elements as unknown[];
  for (const update of elementUpdates) {
    if (!validTypes.has(update.new_type)) {
      return false;
    }
    const match = elements.find((rawElement) => {
      if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
        return false;
      }
      const element = rawElement as EditableStructureElement;
      return element.review_id === update.review_id;
    });
    if (!match) {
      return false;
    }
  }

  return true;
}

export function applyReadingOrderSuggestion(
  structure: Record<string, unknown> | null,
  llmSuggestion: LlmSuggestion | null,
): Record<string, unknown> | null {
  if (!canApplyReadingOrderSuggestion(structure, llmSuggestion) || !structure || !Array.isArray(structure.elements)) {
    return null;
  }

  let nextElements = [...structure.elements];
  const pageOrders = readingOrderPageOrders(llmSuggestion);
  const elementUpdates = readingOrderElementUpdates(llmSuggestion);

  for (const pageOrder of pageOrders) {
    const pageEntries = structureElementsForPage({ ...structure, elements: nextElements }, pageOrder.page);
    const replacementById = new Map(
      pageEntries.map(({ element }) => [element.review_id, element]),
    );
    const replacements = pageOrder.ordered_review_ids.map((reviewId) => replacementById.get(reviewId));
    if (replacements.some((element) => !element)) {
      return null;
    }
    const replacementMap = new Map(
      pageEntries.map(({ index }, position) => [index, replacements[position] as EditableStructureElement]),
    );
    nextElements = nextElements.map((rawElement, index) => replacementMap.get(index) ?? rawElement);
  }

  if (elementUpdates.length > 0) {
    const updatesById = new Map(elementUpdates.map((update) => [update.review_id, update]));
    nextElements = nextElements.map((rawElement) => {
      if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
        return rawElement;
      }
      const element = rawElement as EditableStructureElement;
      const update = updatesById.get(element.review_id);
      if (!update) {
        return rawElement;
      }
      return _applyReadingOrderElementUpdate(element, update);
    });
  }

  return {
    ...structure,
    elements: nextElements,
  };
}

export function canApplyTableSuggestion(
  structure: Record<string, unknown> | null,
  llmSuggestion: LlmSuggestion | null,
): boolean {
  if (!structure || !Array.isArray(structure.elements)) {
    return false;
  }
  const updates = tableHeaderUpdates(llmSuggestion);
  if (updates.length === 0) {
    return false;
  }

  for (const update of updates) {
    const match = (structure.elements as unknown[]).find((rawElement) => {
      if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
        return false;
      }
      const element = rawElement as EditableStructureElement;
      if (element.review_id !== update.table_review_id || element.type !== "table") {
        return false;
      }
      const page = typeof element.page === "number" ? element.page + 1 : undefined;
      return typeof update.page !== "number" || page === update.page;
    });
    if (!match || !Array.isArray((match as Record<string, unknown>).cells)) {
      return false;
    }
  }
  return true;
}

export function canApplySingleTableSuggestion(
  structure: Record<string, unknown> | null,
  update: TableHeaderUpdate | null,
): boolean {
  if (!update) {
    return false;
  }
  return canApplyTableSuggestion(structure, { proposed_table_updates: [update] });
}

export function applyTableSuggestion(
  structure: Record<string, unknown> | null,
  llmSuggestion: LlmSuggestion | null,
): Record<string, unknown> | null {
  if (!canApplyTableSuggestion(structure, llmSuggestion) || !structure || !Array.isArray(structure.elements)) {
    return null;
  }
  const updatesById = new Map(tableHeaderUpdates(llmSuggestion).map((update) => [update.table_review_id, update]));
  const nextElements = (structure.elements as unknown[]).map((rawElement) => {
    if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
      return rawElement;
    }
    const element = rawElement as EditableStructureElement;
    const update = updatesById.get(element.review_id);
    if (!update || element.type !== "table" || !Array.isArray(element.cells)) {
      return rawElement;
    }

    const headerRows = new Set(update.header_rows);
    const rowHeaderColumns = new Set(update.row_header_columns);
    const nextCells = (element.cells as unknown[]).map((rawCell) => {
      if (!rawCell || typeof rawCell !== "object" || Array.isArray(rawCell)) {
        return rawCell;
      }
      const cell = rawCell as Record<string, unknown>;
      const row = typeof cell.row === "number" ? cell.row : Number(cell.row ?? -1);
      const col = typeof cell.col === "number" ? cell.col : Number(cell.col ?? -1);
      const columnHeader = Number.isFinite(row) && headerRows.has(row);
      const rowHeader = Number.isFinite(col) && rowHeaderColumns.has(col);
      return {
        ...cell,
        column_header: columnHeader,
        row_header: rowHeader,
        is_header: columnHeader || rowHeader,
      };
    });

    return {
      ...element,
      cells: nextCells,
    };
  });

  return {
    ...structure,
    elements: nextElements,
  };
}

export function applySingleTableSuggestion(
  structure: Record<string, unknown> | null,
  update: TableHeaderUpdate | null,
): Record<string, unknown> | null {
  if (!update) {
    return null;
  }
  return applyTableSuggestion(structure, { proposed_table_updates: [update] });
}

export function structureTypeLabel(type: string | undefined): string {
  const option = STRUCTURE_TYPE_OPTIONS.find((entry) => entry.value === type);
  return option?.label ?? String(type ?? "paragraph");
}

export function guidanceForTask(taskType: string): string[] {
  if (taskType === "reading_order") {
    return [
      "Read the document with a screen reader or exported text view.",
      "Check that headings, paragraphs, lists, and sidebars are announced in the intended order.",
    ];
  }
  if (taskType === "font_text_fidelity") {
    return [
      "Compare the visible text against what copy and paste or a screen reader exposes.",
      "Pay attention to symbols, ligatures, math, and unusual fonts.",
    ];
  }
  if (taskType === "table_semantics") {
    return [
      "Check header cells, merged cells, and reading order row by row.",
      "Confirm that a screen reader can identify the right headers for each data cell.",
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
