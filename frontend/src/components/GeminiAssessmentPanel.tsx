import { useState } from "react";

import type { ReviewTask } from "../types";
import type {
  DocumentOverlay,
  LlmSuggestion,
  ReadingOrderElementUpdate,
  ReadingOrderPageOrder,
  TableReviewTarget,
} from "../pages/reviewHelpers";

interface GeminiAssessmentPanelProps {
  task: ReviewTask;
  llmSuggestion: LlmSuggestion | null;
  supportsSuggestion: boolean;
  suggestingTask: number | null;
  suggestReviewTaskPending: boolean;
  suggestReviewTaskError: Error | null;
  suggestErrorTask: number | null;
  documentOverlay: DocumentOverlay | null;
  suggestionGeneratedAt: string | null;
  suggestedPageOrders: ReadingOrderPageOrder[];
  suggestedElementUpdates: ReadingOrderElementUpdate[];
  tableTargets: TableReviewTarget[];
  suggestedActualTextCandidates: Array<{
    page: number;
    operator_index: number;
    font?: string;
    proposed_actualtext: string;
    confidence?: string;
    reason?: string;
  }>;
  canAcceptRecommendation: boolean;
  acceptRecommendationPending?: boolean;
  acceptRecommendationError?: Error | null;
  onAcceptRecommendation: (task: ReviewTask) => Promise<void> | void;
  onSuggestTask: (task: ReviewTask, feedback?: string) => void;
}

interface SuggestionRevisionFormProps {
  task: ReviewTask;
  llmSuggestion: LlmSuggestion;
  suggestingTask: number | null;
  suggestReviewTaskPending: boolean;
  onSuggestTask: (task: ReviewTask, feedback?: string) => void;
}

function SuggestionRevisionForm({
  task,
  llmSuggestion,
  suggestingTask,
  suggestReviewTaskPending,
  onSuggestTask,
}: SuggestionRevisionFormProps) {
  const [feedbackDraft, setFeedbackDraft] = useState("");
  const trimmedFeedback = feedbackDraft.trim();
  const isRevising = suggestingTask === task.id && trimmedFeedback.length > 0;

  return (
    <div className="rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
      <p className="text-xs font-semibold text-ink">Suggest alternative</p>
      <textarea
        value={feedbackDraft}
        onChange={(e) => setFeedbackDraft(e.target.value)}
        rows={3}
        placeholder="Example: This sidebar belongs after the paragraph below it. The first column is not a row header. This icon is part of the screenshot and should not be read separately."
        className="
          mt-3 w-full rounded-lg border border-ink/10 bg-paper-warm/40 px-3 py-2
          text-sm text-ink placeholder:text-ink-muted/70
          focus:outline-none focus:ring-2 focus:ring-accent/20
        "
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => onSuggestTask(task, trimmedFeedback)}
          disabled={trimmedFeedback.length === 0 || suggestReviewTaskPending}
          className="
            px-3 py-2 rounded-lg text-xs font-medium
            bg-accent text-white
            hover:bg-accent/90 transition-colors
            disabled:opacity-50 disabled:cursor-not-allowed
          "
        >
          {isRevising ? "Revising..." : "Revise Recommendation"}
        </button>
        {llmSuggestion.reviewer_feedback && (
          <span className="text-xs text-ink-muted">
            Last revision used your note: "{llmSuggestion.reviewer_feedback}"
          </span>
        )}
      </div>
    </div>
  );
}

export default function GeminiAssessmentPanel({
  task,
  llmSuggestion,
  supportsSuggestion,
  suggestingTask,
  suggestReviewTaskPending,
  suggestReviewTaskError,
  suggestErrorTask,
  documentOverlay,
  suggestionGeneratedAt,
  suggestedPageOrders,
  suggestedElementUpdates,
  tableTargets,
  suggestedActualTextCandidates,
  canAcceptRecommendation,
  acceptRecommendationPending = false,
  acceptRecommendationError = null,
  onAcceptRecommendation,
  onSuggestTask,
}: GeminiAssessmentPanelProps) {
  return (
    <div className="mt-4 rounded-lg border border-accent-light bg-accent-glow/60 px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-semibold text-ink">Recommendation</p>
        {supportsSuggestion && (
          <button
            type="button"
            onClick={() => onSuggestTask(task)}
            disabled={suggestingTask === task.id || suggestReviewTaskPending}
            className="
              px-3 py-2 rounded-lg text-xs font-medium
              bg-accent text-white
              hover:bg-accent/90 transition-colors
              disabled:opacity-50 disabled:cursor-not-allowed
            "
          >
            {suggestingTask === task.id
              ? "Analyzing..."
              : llmSuggestion
                ? "Refresh Recommendation"
                : "Generate Recommendation"}
          </button>
        )}
      </div>

      {llmSuggestion ? (
        <div className="mt-3 space-y-3">
          {llmSuggestion.summary && (
            <p className="text-sm text-ink leading-relaxed">{llmSuggestion.summary}</p>
          )}
          {llmSuggestion.reason && (
            <p className="text-xs text-ink-light leading-relaxed">{llmSuggestion.reason}</p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`
                text-[11px] px-2 py-0.5 rounded-full font-medium
                ${llmSuggestion.confidence === "high"
                  ? "bg-success-light text-success"
                  : llmSuggestion.confidence === "low"
                    ? "bg-error-light text-error"
                    : "bg-warning-light text-warning"}
              `}
            >
              {String(llmSuggestion.confidence ?? "unknown")} confidence
            </span>
            <span className="text-[11px] px-2 py-0.5 rounded-full font-medium bg-paper-warm text-ink-muted">
              {String(llmSuggestion.suggested_action ?? "manual_only").replaceAll("_", " ")}
            </span>
          </div>

          <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
            <div className="flex flex-wrap items-center gap-2">
              {canAcceptRecommendation ? (
                <button
                  type="button"
                  onClick={() => onAcceptRecommendation(task)}
                  disabled={acceptRecommendationPending}
                  className="
                    px-3 py-2 rounded-lg text-xs font-medium
                    bg-accent text-white
                    hover:bg-accent/90 transition-colors
                    disabled:opacity-50 disabled:cursor-not-allowed
                  "
                >
                  {acceptRecommendationPending ? "Applying..." : "Accept Recommendation"}
                </button>
              ) : (
                <span className="rounded-full bg-paper-warm px-2 py-1 text-[11px] font-medium text-ink-muted">
                  Revise before accepting
                </span>
              )}
              <span className="text-xs text-ink-muted">
                {canAcceptRecommendation
                  ? "If this is close but not right, describe what is wrong below."
                  : "This recommendation still needs another pass before the app can accept it directly."}
              </span>
            </div>
            {acceptRecommendationError && (
              <p className="mt-2 text-xs text-error">
                {acceptRecommendationError.message || "Failed to apply the recommendation"}
              </p>
            )}
          </div>

          {task.task_type === "reading_order" && (
            <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
              <p className="text-xs font-semibold text-ink">Recommended changes</p>
              {(suggestedPageOrders.length > 0 || suggestedElementUpdates.length > 0) ? (
                <div className="mt-3 space-y-1.5">
                  {suggestedPageOrders.map((order) => (
                    <p key={`${task.id}-ro-page-${order.page}`} className="text-xs text-ink-light">
                      Page {order.page}: reorder {order.ordered_review_ids.length} elements
                      {order.reason ? ` — ${order.reason}` : ""}
                    </p>
                  ))}
                  {suggestedElementUpdates.map((update, i) => (
                    <p key={`${task.id}-ro-update-${i}`} className="text-xs text-ink-light">
                      {update.page ? `Page ${update.page}: ` : ""}
                      Change to{" "}
                      <span className="font-medium">
                        {update.new_type}
                        {update.new_level ? ` ${update.new_level}` : ""}
                      </span>
                      {update.reason ? ` — ${update.reason}` : ""}
                    </p>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-xs text-ink-muted">
                  No concrete reading-order edits were proposed.
                </p>
              )}
            </div>
          )}

          {task.task_type === "font_text_fidelity" && suggestedActualTextCandidates.length > 0 && (
            <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
              <p className="text-xs font-semibold text-ink">Suggested spoken-text fix</p>
              <div className="mt-3 space-y-2">
                {suggestedActualTextCandidates.map((candidate, index) => (
                  <div
                    key={`${task.id}-batch-candidate-${index}`}
                    className="rounded-lg bg-paper-warm/70 px-3 py-2"
                  >
                    <p className="text-sm text-ink">
                      Page {candidate.page} · operator {candidate.operator_index}
                      {candidate.font ? ` · ${candidate.font}` : ""}
                    </p>
                    <p className="mt-1 text-sm text-ink break-words">
                      {candidate.proposed_actualtext}
                    </p>
                    <p className="mt-1 text-xs text-ink-muted">
                      {candidate.confidence ? `${candidate.confidence} confidence` : "Confidence not provided"}
                      {candidate.reason ? ` · ${candidate.reason}` : ""}
                    </p>
                  </div>
                ))}
              </div>
              <p className="mt-3 text-xs text-ink-muted">
                Accepting the recommendation applies the suggested spoken-text fixes directly.
              </p>
            </div>
          )}

          {task.task_type === "table_semantics" && (
            <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
              <p className="text-xs font-semibold text-ink">Recommended table interpretation</p>
              <p className="mt-2 text-xs text-ink-muted">
                {tableTargets.length} flagged table{tableTargets.length === 1 ? "" : "s"} reviewed.
              </p>
              {llmSuggestion.proposed_table_updates && llmSuggestion.proposed_table_updates.length > 0 ? (
                <div className="mt-3 space-y-1.5">
                  {llmSuggestion.proposed_table_updates.map((update, index) => (
                    <p key={`${task.id}-table-update-${index}`} className="text-xs text-ink-light">
                      {typeof update.page === "number" ? `Page ${update.page}: ` : ""}
                      {update.table_review_id ? `${update.table_review_id} · ` : ""}
                      {update.row_header_columns && update.row_header_columns.length > 0
                        ? `row headers ${update.row_header_columns.join(", ")}`
                        : "no row headers"}
                      {update.header_rows && update.header_rows.length > 0
                        ? ` · header rows ${update.header_rows.join(", ")}`
                        : " · no header rows"}
                      {update.reason ? ` — ${update.reason}` : ""}
                    </p>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-xs text-ink-muted">
                  No concrete table-header updates were proposed.
                </p>
              )}
            </div>
          )}

          <SuggestionRevisionForm
            key={`${task.id}:${llmSuggestion.generated_at ?? "initial"}`}
            task={task}
            llmSuggestion={llmSuggestion}
            suggestingTask={suggestingTask}
            suggestReviewTaskPending={suggestReviewTaskPending}
            onSuggestTask={onSuggestTask}
          />

          {((documentOverlay && documentOverlay.pages.length > 0)
            || (llmSuggestion.review_focus && llmSuggestion.review_focus.length > 0)
            || suggestionGeneratedAt) && (
            <details className="rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
              <summary className="cursor-pointer text-xs font-semibold text-ink">
                Why the model thinks this
              </summary>
              <div className="mt-3 space-y-3">
                {documentOverlay && documentOverlay.pages.length > 0 && (
                  <div className="space-y-3">
                    {documentOverlay.pages.map((page) => (
                      <div
                        key={`${task.id}-overlay-page-${page.page_number}`}
                        className="rounded-lg bg-paper-warm/70 px-3 py-3"
                      >
                        <p className="text-sm font-medium text-ink">Page {page.page_number}</p>
                        {page.blocks.length > 0 && (
                          <p className="mt-1 text-xs text-ink-muted">
                            {page.blocks.length} block{page.blocks.length === 1 ? "" : "s"}
                            {" · "}
                            {page.blocks.filter((block) => Boolean(block.semantic_text_hint)).length} semantic hint
                            {page.blocks.filter((block) => Boolean(block.semantic_text_hint)).length === 1 ? "" : "s"}
                          </p>
                        )}
                        {page.tables.length > 0 && (
                          <p className="mt-1 text-xs text-ink-muted">
                            {page.tables.length} table interpretation{page.tables.length === 1 ? "" : "s"}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {llmSuggestion.review_focus && llmSuggestion.review_focus.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-xs font-semibold text-ink">Focus areas</p>
                    {llmSuggestion.review_focus.map((item, index) => (
                      <div
                        key={`${task.id}-focus-${index}`}
                        className="rounded-lg bg-paper-warm/70 px-3 py-2"
                      >
                        <p className="text-xs text-ink">
                          {item.page ? `Page ${item.page}` : "Page unknown"}
                          {item.font ? ` · ${item.font}` : ""}
                          {item.rule_id ? ` · ${item.rule_id}` : ""}
                        </p>
                        {item.recommended_reviewer_action && (
                          <p className="mt-1 text-xs text-ink-muted">{item.recommended_reviewer_action}</p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {suggestionGeneratedAt && (
                  <p className="text-[11px] text-ink-muted">
                    Generated {suggestionGeneratedAt}
                  </p>
                )}
              </div>
            </details>
          )}
        </div>
      ) : (
        <p className="mt-3 text-xs text-ink-muted">
          No recommendation yet.
        </p>
      )}

      {suggestReviewTaskError && suggestErrorTask === task.id && (
        <p className="mt-3 text-xs text-error">
          {suggestReviewTaskError.message || "Failed to generate recommendation"}
        </p>
      )}
    </div>
  );
}
