import type { ReviewTask } from "../types";

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
  reviewer_feedback?: string;
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
    suggested_action?: string;
    resolved_kind?: string;
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

export type ReadingOrderTextHint = {
  page?: number;
  review_id: string;
  extracted_text?: string;
  readable_text_hint?: string;
  suggested_action?: string;
  resolved_kind?: string;
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
  semantic_resolved_kind?: string;
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

export const LLM_SUGGESTION_TASK_TYPES = new Set(["font_text_fidelity", "reading_order", "table_semantics"]);

// ---------------------------------------------------------------------------
// Pure helper functions
// ---------------------------------------------------------------------------

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
      suggested_action: typeof item.suggested_action === "string" ? item.suggested_action : undefined,
      resolved_kind: typeof item.resolved_kind === "string" ? item.resolved_kind : undefined,
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
              semantic_resolved_kind: typeof block.semantic_resolved_kind === "string" ? block.semantic_resolved_kind : undefined,
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

export function canAcceptRecommendation(
  task: ReviewTask,
  llmSuggestion: LlmSuggestion | null,
): boolean {
  if (!llmSuggestion) {
    return false;
  }

  const suggestedAction =
    typeof llmSuggestion.suggested_action === "string"
      ? llmSuggestion.suggested_action.trim()
      : "";

  if (task.task_type === "reading_order") {
    return suggestedAction === "confirm_current_order"
      || readingOrderPageOrders(llmSuggestion).length > 0
      || readingOrderElementUpdates(llmSuggestion).length > 0;
  }

  if (task.task_type === "table_semantics") {
    if (suggestedAction === "confirm_current_headers") {
      return true;
    }
    if (suggestedAction !== "set_table_headers") {
      return false;
    }
    return llmSuggestion.proposed_table_updates?.some(
      (update) => typeof update.table_review_id === "string" && update.table_review_id.trim().length > 0,
    ) ?? false;
  }

  if (task.task_type === "font_text_fidelity") {
    if (applicableActualTextCandidates(llmSuggestion, fontReviewTargets(task)).length > 0) {
      return true;
    }
    const highConfidence = llmSuggestion.confidence === "high";
    return (
      highConfidence
      && (suggestedAction === "font_map_candidate" || suggestedAction === "artifact_if_decorative")
      && fontReviewTargets(task).length > 0
    );
  }

  return false;
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
