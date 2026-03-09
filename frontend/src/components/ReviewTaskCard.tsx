import type { ReviewTask } from "../types";
import type { FontReviewTarget, LlmSuggestion } from "../pages/reviewHelpers";
import {
  canApplySingleTableSuggestion,
  canApplyTableSuggestion,
  documentOverlayForSuggestion,
  LLM_SUGGESTION_TASK_TYPES,
  canApplyReadingOrderSuggestion,
  applicableActualTextCandidates,
  evidenceFieldsForTask,
  existingEvidenceForTask,
  fontReviewTargets,
  guidanceForTask,
  llmSuggestionForTask,
  metadataEntriesForTask,
  numberListMetadata,
  pagePreviewUrl,
  previewPagesForTask,
  readingOrderElementUpdates,
  readingOrderPageOrders,
  readingOrderTextHints,
  stringListMetadata,
  tableHeaderUpdateForTarget,
  tableHeaderUpdates,
  tableTargetPreviewUrl,
  tableReviewTargets,
  structureTypeLabel,
  structureElementsForPage,
  structurePages,
} from "../pages/reviewHelpers";
import FontTargetPanel from "./FontTargetPanel";
import StructureEditor from "./StructureEditor";

// ---------------------------------------------------------------------------
// Mutation status interfaces
// ---------------------------------------------------------------------------

export interface MutationStatus {
  isPending: boolean;
  error: Error | null;
  key?: string | number | null;
}

export interface FontMutationState {
  actualText: MutationStatus;
  unicodeMapping: MutationStatus;
  actualTextBatch: MutationStatus;
  applyingActualTextKey: string | null;
  applyingFontMapKey: string | null;
}

export interface StructureMutationState {
  update: MutationStatus;
  historyLength: number;
  futureLength: number;
}

export interface TaskMutationState {
  updateTask: MutationStatus;
  suggestTask: MutationStatus;
  savingTask: number | null;
  suggestingTask: number | null;
  suggestErrorTask: number | null;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ReviewTaskCardProps {
  jobId: string;
  task: ReviewTask;
  resolutionNote: string;
  missingEvidence: string[];
  canResolve: boolean;

  // Font target state
  actualTextDrafts: Record<string, string>;
  fontMutation: FontMutationState;

  // Structure editor state
  editingStructure: Record<string, unknown> | null;
  selectedReadingOrderPage: number | null;
  structureMutation: StructureMutationState;

  // Task action state
  taskMutation: TaskMutationState;

  // Callbacks
  onResolutionNoteChange: (taskId: number, value: string) => void;
  onEvidenceChange: (taskId: number, existingEvidence: Record<string, string>, key: string, value: string) => void;
  onActualTextDraftChange: (key: string, value: string) => void;
  onApplyActualText: (task: ReviewTask, target: FontReviewTarget) => void;
  onApplyFontMap: (task: ReviewTask, target: FontReviewTarget) => void;
  onUseSuggestedActualText: (task: ReviewTask, target: FontReviewTarget, proposedText: string) => void;
  onApplySuggestedBatch: (task: ReviewTask, llmSuggestion: LlmSuggestion | null, targets: FontReviewTarget[]) => void;
  onApplyReadingOrderSuggestion: (task: ReviewTask, llmSuggestion: LlmSuggestion | null) => void;
  onApplyTableSuggestion: (task: ReviewTask, llmSuggestion: LlmSuggestion | null, tableReviewId?: string) => void;
  onUpdateTask: (task: ReviewTask, status: "pending_review" | "resolved") => void;
  onSuggestTask: (task: ReviewTask) => void;
  onSelectReadingOrderPage: (page: number) => void;
  onUndoStructure: () => void;
  onRedoStructure: () => void;
  onResetReadingOrderPage: (page: number) => void;
  onMoveElement: (page: number, reviewId: string, direction: -1 | 1) => void;
  onToggleArtifact: (reviewId: string) => void;
  onUpdateElementType: (reviewId: string, nextType: string) => void;
  onUpdateHeadingLevel: (reviewId: string, level: number) => void;
  onSaveStructure: () => void;

  /** Called to get the evidence value for a task+field key */
  evidenceValueForTask: (task: ReviewTask, evidenceKey: string) => string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ReviewTaskCard({
  jobId,
  task,
  resolutionNote,
  missingEvidence,
  canResolve,
  actualTextDrafts,
  fontMutation,
  editingStructure,
  selectedReadingOrderPage,
  structureMutation,
  taskMutation,
  onResolutionNoteChange,
  onEvidenceChange,
  onActualTextDraftChange,
  onApplyActualText,
  onApplyFontMap,
  onUseSuggestedActualText,
  onApplySuggestedBatch,
  onApplyReadingOrderSuggestion,
  onApplyTableSuggestion,
  onUpdateTask,
  onSuggestTask,
  onSelectReadingOrderPage,
  onUndoStructure,
  onRedoStructure,
  onResetReadingOrderPage,
  onMoveElement,
  onToggleArtifact,
  onUpdateElementType,
  onUpdateHeadingLevel,
  onSaveStructure,
  evidenceValueForTask,
}: ReviewTaskCardProps) {
  // Destructure only the font mutation state used directly in this component
  const {
    actualTextBatch: { isPending: applyFontActualTextBatchPending, error: applyFontActualTextBatchError },
  } = fontMutation;
  const applyingActualTextBatchTaskId = fontMutation.actualTextBatch.key as number | null;
  const {
    update: { isPending: updateStructurePending, error: updateStructureError },
    historyLength: structureHistoryLength,
    futureLength: structureFutureLength,
  } = structureMutation;
  const {
    savingTask,
    suggestingTask,
    suggestErrorTask,
    updateTask: { isPending: updateReviewTaskPending, error: updateReviewTaskError },
    suggestTask: { isPending: suggestReviewTaskPending, error: suggestReviewTaskError },
  } = taskMutation;
  const metaEntries = metadataEntriesForTask(task);
  const evidenceFields = evidenceFieldsForTask(task.task_type);
  const pagesToCheck = numberListMetadata(task, "pages_to_check");
  const fontsToCheck = stringListMetadata(task, "fonts_to_check");
  const fontRuleIds = stringListMetadata(task, "font_rule_ids");
  const reviewTargets = fontReviewTargets(task);
  const llmSuggestion = llmSuggestionForTask(task);
  const suggestedPageOrders = readingOrderPageOrders(llmSuggestion);
  const suggestedElementUpdates = readingOrderElementUpdates(llmSuggestion);
  const suggestedTextHints = readingOrderTextHints(llmSuggestion);
  const suggestedTableUpdates = tableHeaderUpdates(llmSuggestion);
  const documentOverlay = documentOverlayForSuggestion(llmSuggestion);
  const tableTargets = tableReviewTargets(task);
  const canApplyReadingOrder =
    task.task_type === "reading_order"
    && canApplyReadingOrderSuggestion(editingStructure, llmSuggestion);
  const canApplyTable =
    task.task_type === "table_semantics"
    && canApplyTableSuggestion(editingStructure, llmSuggestion);
  const suggestedActualTextCandidates = applicableActualTextCandidates(llmSuggestion, reviewTargets);
  const supportsSuggestion = LLM_SUGGESTION_TASK_TYPES.has(task.task_type);
  const suggestionGeneratedAt = llmSuggestion?.generated_at
    ? new Date(llmSuggestion.generated_at).toLocaleString()
    : null;
  const previewPages = previewPagesForTask(task, llmSuggestion);

  // Structure editor data
  const editablePages = structurePages(editingStructure);
  const editorPage =
    selectedReadingOrderPage && editablePages.includes(selectedReadingOrderPage)
      ? selectedReadingOrderPage
      : previewPages[0] ?? editablePages[0] ?? null;
  const pageElements =
    task.task_type === "reading_order" && editorPage
      ? structureElementsForPage(editingStructure, editorPage)
      : [];
  const hasUnsavedStructureEdits = structureHistoryLength > 0;
  const pagePreviewTaskType = task.task_type === "table_semantics" ? "Specific table pages" : "Relevant pages";

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      {/* Header: title, severity, source badges */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{task.title}</h3>
          <p className="text-sm text-ink-muted mt-1">{task.detail}</p>
        </div>
        <div className="flex flex-col items-end gap-2 shrink-0">
          <span
            className={`
              text-[11px] px-2 py-1 rounded-full
              ${
                task.blocking
                  ? "bg-error-light text-error"
                  : "bg-warning-light text-warning"
              }
            `}
          >
            {task.blocking ? "Must Fix" : "Optional Check"}
          </span>
          <span className="text-xs text-ink-muted capitalize">
            {task.severity} severity
          </span>
          <span className="text-xs text-ink-muted capitalize">
            {task.source === "validation" ? "Compliance check" : task.source === "fidelity" ? "Content check" : task.source}
          </span>
        </div>
      </div>

      {/* Review focus: pages, fonts, rules */}
      {(pagesToCheck.length > 0 || fontsToCheck.length > 0 || fontRuleIds.length > 0) && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/60 px-3 py-3">
          <p className="text-xs font-semibold text-ink mb-2">Review focus</p>
          <div className="grid gap-3 md:grid-cols-3">
            {pagesToCheck.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                  Pages
                </p>
                <p className="text-sm text-ink mt-1">
                  {pagesToCheck.join(", ")}
                </p>
              </div>
            )}
            {fontsToCheck.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                  Fonts
                </p>
                <p className="text-sm text-ink mt-1">
                  {fontsToCheck.join(", ")}
                </p>
              </div>
            )}
            {fontRuleIds.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                  Rules
                </p>
                <p className="text-sm text-ink mt-1 break-words">
                  {fontRuleIds.join(", ")}
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Relevant page previews */}
      {previewPages.length > 0 && (
        <div className="mt-4">
          <p className="text-xs font-semibold text-ink mb-2">{pagePreviewTaskType}</p>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {previewPages.map((pageNumber) => (
              <a
                key={`${task.id}-page-${pageNumber}`}
                href={pagePreviewUrl(jobId, pageNumber)}
                target="_blank"
                rel="noreferrer"
                className="block rounded-lg border border-ink/8 bg-white/70 p-2 no-underline"
              >
                <p className="text-xs font-semibold text-ink mb-2">
                  Page {pageNumber}
                </p>
                <img
                  src={pagePreviewUrl(jobId, pageNumber)}
                  alt={`Preview of page ${pageNumber}`}
                  loading="lazy"
                  className="w-full rounded-md border border-ink/6 bg-paper-warm object-cover"
                />
              </a>
            ))}
          </div>
        </div>
      )}

      {/* Font target panel */}
      <FontTargetPanel
        jobId={jobId}
        task={task}
        reviewTargets={reviewTargets}
        llmSuggestion={llmSuggestion}
        actualTextDrafts={actualTextDrafts}
        fontMutation={fontMutation}
        onActualTextDraftChange={onActualTextDraftChange}
        onApplyActualText={onApplyActualText}
        onApplyFontMap={onApplyFontMap}
        onUseSuggestedActualText={onUseSuggestedActualText}
      />

      {/* Metadata entries */}
      {metaEntries.length > 0 && (
        <p className="text-xs text-ink-muted mt-3 font-mono">
          {metaEntries
            .map(([key, value]) => `${key}=${value}`)
            .join(" | ")}
        </p>
      )}

      {/* Review checklist / guidance */}
      <div className="mt-4 rounded-lg bg-paper-warm/60 px-3 py-3">
        <p className="text-xs font-semibold text-ink mb-2">Review checklist</p>
        <div className="space-y-1">
          {guidanceForTask(task.task_type).map((item, index) => (
            <p key={`${task.id}-guidance-${index}`} className="text-xs text-ink-muted">
              {index + 1}. {item}
            </p>
          ))}
        </div>
      </div>

      {/* Structure editor (reading_order tasks only) */}
      {task.task_type === "reading_order" && (
        <StructureEditor
          taskId={task.id}
          editablePages={editablePages}
          editorPage={editorPage}
          editorPagePreviewUrl={editorPage ? pagePreviewUrl(jobId, editorPage) : null}
          pageElements={pageElements}
          readableTextHints={suggestedTextHints.filter((hint) => hint.page === editorPage)}
          hasUnsavedEdits={hasUnsavedStructureEdits}
          structureHistoryLength={structureHistoryLength}
          structureFutureLength={structureFutureLength}
          updateStructurePending={updateStructurePending}
          updateStructureError={updateStructureError}
          onSelectPage={onSelectReadingOrderPage}
          onUndo={onUndoStructure}
          onRedo={onRedoStructure}
          onResetPage={onResetReadingOrderPage}
          onMoveElement={onMoveElement}
          onToggleArtifact={onToggleArtifact}
          onUpdateElementType={onUpdateElementType}
          onUpdateHeadingLevel={onUpdateHeadingLevel}
          onSave={onSaveStructure}
        />
      )}

      {/* LLM suggestion panel */}
      {(supportsSuggestion || llmSuggestion) && (
        <div className="mt-4 rounded-lg border border-accent-light bg-accent-glow/60 px-3 py-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold text-ink">Gemini suggestion</p>
              <p className="text-xs text-ink-muted mt-1">
                Proposal only. Review it, then choose whether to apply the suggested fix.
              </p>
            </div>
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
                {suggestingTask === task.id ? "Analyzing..." : llmSuggestion ? "Refresh Suggestion" : "Suggest Fix"}
              </button>
            )}
          </div>
          {llmSuggestion ? (
            <div className="mt-3 space-y-3">
              <div className="grid gap-3 md:grid-cols-3">
                <div className="rounded-lg bg-white/70 px-3 py-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                    Action
                  </p>
                  <p className="mt-1 text-sm text-ink break-words">
                    {String(llmSuggestion.suggested_action ?? "manual_only").replaceAll("_", " ")}
                  </p>
                </div>
                <div className="rounded-lg bg-white/70 px-3 py-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                    Confidence
                  </p>
                  <p className="mt-1 text-sm text-ink capitalize">
                    {String(llmSuggestion.confidence ?? "unknown")}
                  </p>
                </div>
                <div className="rounded-lg bg-white/70 px-3 py-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                    Model
                  </p>
                  <p className="mt-1 text-sm text-ink break-words">
                    {llmSuggestion.model ?? "unknown"}
                  </p>
                </div>
              </div>
              {llmSuggestion.summary && (
                <p className="text-sm text-ink">{llmSuggestion.summary}</p>
              )}
              {llmSuggestion.reason && (
                <p className="text-xs text-ink-muted">{llmSuggestion.reason}</p>
              )}
              {documentOverlay && documentOverlay.pages.length > 0 && (
                <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-semibold text-ink">
                        Gemini page model
                      </p>
                      <p className="mt-1 text-xs text-ink-muted">
                        This is the merged page interpretation the app derived from Gemini&apos;s structured proposal.
                      </p>
                    </div>
                    <span className="rounded-full bg-accent-glow px-2 py-1 text-[11px] text-accent">
                      {documentOverlay.provenance ?? "gemini"}
                    </span>
                  </div>
                  <div className="mt-3 space-y-3">
                    {documentOverlay.pages.map((page) => (
                      <div
                        key={`${task.id}-overlay-page-${page.page_number}`}
                        className="rounded-lg bg-paper-warm/70 px-3 py-3"
                      >
                        <p className="text-sm font-medium text-ink">
                          Page {page.page_number}
                        </p>
                        {page.blocks.length > 0 && (
                          <div className="mt-3">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                              Block order
                            </p>
                            <div className="mt-2 space-y-2">
                              {page.blocks.map((block, index) => (
                                <div
                                  key={`${task.id}-overlay-block-${page.page_number}-${block.review_id}`}
                                  className="rounded-lg bg-white/70 px-3 py-2"
                                >
                                  <p className="text-xs font-medium text-ink">
                                    {index + 1}. {structureTypeLabel(block.role ?? "paragraph")}
                                    {typeof block.level === "number" ? ` (H${block.level})` : ""}
                                    {" · "}
                                    {block.review_id}
                                  </p>
                                  {block.text && (
                                    <p className="mt-1 text-xs text-ink break-words">{block.text}</p>
                                  )}
                                  {block.semantic_text_hint && (
                                    <div className="mt-2 rounded-lg border border-accent-light bg-accent-glow/40 px-2 py-2">
                                      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                        Gemini readable text
                                      </p>
                                      <p className="mt-1 text-xs text-ink break-words">
                                        {block.semantic_text_hint}
                                      </p>
                                      <p className="mt-1 text-[11px] text-ink-muted">
                                        {block.semantic_issue_type
                                          ? block.semantic_issue_type.replaceAll("_", " ")
                                          : "semantic hint"}
                                        {block.semantic_blocking ? " · blocking" : ""}
                                      </p>
                                    </div>
                                  )}
                                  <p className="mt-1 text-[11px] text-ink-muted">
                                    {block.provenance ?? "unknown"}
                                    {typeof block.confidence === "number"
                                      ? ` · ${Math.round(block.confidence * 100)}% confidence`
                                      : ""}
                                  </p>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {page.tables.length > 0 && (
                          <div className="mt-3">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                              Table semantics
                            </p>
                            <div className="mt-2 space-y-2">
                              {page.tables.map((table) => (
                                <div
                                  key={`${task.id}-overlay-table-${page.page_number}-${table.table_review_id}`}
                                  className="rounded-lg bg-white/70 px-3 py-2"
                                >
                                  <p className="text-xs font-medium text-ink">
                                    {table.table_review_id}
                                  </p>
                                  <p className="mt-1 text-xs text-ink-muted">
                                    Header rows: {table.header_rows.length > 0 ? table.header_rows.join(", ") : "none"}
                                    {" · "}
                                    Row-header columns: {table.row_header_columns.length > 0 ? table.row_header_columns.join(", ") : "none"}
                                  </p>
                                  <p className="mt-1 text-[11px] text-ink-muted">
                                    {table.provenance ?? "unknown"}
                                    {typeof table.confidence === "number"
                                      ? ` · ${Math.round(table.confidence * 100)}% confidence`
                                      : ""}
                                  </p>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {task.task_type === "font_text_fidelity" && suggestedActualTextCandidates.length > 0 && (
                <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-semibold text-ink">
                        Suggested spoken-text batch
                      </p>
                      <p className="mt-1 text-xs text-ink-muted">
                        Applies the suggested spoken text to all flagged locations in one pass, then reruns the checks once.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onApplySuggestedBatch(task, llmSuggestion, reviewTargets)}
                      disabled={applyingActualTextBatchTaskId === task.id || applyFontActualTextBatchPending}
                      className="
                        px-3 py-2 rounded-lg text-xs font-medium
                        bg-accent text-white
                        hover:bg-accent/90 transition-colors
                        disabled:opacity-50 disabled:cursor-not-allowed
                      "
                    >
                      {applyingActualTextBatchTaskId === task.id ? "Applying..." : "Apply Suggested Text"}
                    </button>
                  </div>
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
                  {applyFontActualTextBatchError && applyingActualTextBatchTaskId === task.id && (
                    <p className="mt-3 text-xs text-error">
                      {applyFontActualTextBatchError.message || "Failed to apply ActualText batch remediation"}
                    </p>
                  )}
                </div>
              )}
              {task.task_type === "reading_order" && (
                <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-semibold text-ink">
                        Suggested reading order changes
                      </p>
                      <p className="mt-1 text-xs text-ink-muted">
                        Load Gemini&apos;s suggested page order and content-type changes into the editor, then review and save them explicitly.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onApplyReadingOrderSuggestion(task, llmSuggestion)}
                      disabled={!canApplyReadingOrder || updateStructurePending}
                      className="
                        px-3 py-2 rounded-lg text-xs font-medium
                        bg-accent text-white
                        hover:bg-accent/90 transition-colors
                        disabled:opacity-50 disabled:cursor-not-allowed
                      "
                    >
                      Load into Editor
                    </button>
                  </div>
                  <div className="mt-3 grid gap-3 md:grid-cols-2">
                    <div className="rounded-lg bg-paper-warm/70 px-3 py-3">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                        Page reorders
                      </p>
                      {suggestedPageOrders.length > 0 ? (
                        <div className="mt-2 space-y-2">
                          {suggestedPageOrders.map((pageOrder) => (
                            <div key={`${task.id}-page-order-${pageOrder.page}`} className="text-xs text-ink">
                              <p className="font-medium">
                                Page {pageOrder.page}: {pageOrder.ordered_review_ids.length} blocks
                              </p>
                              {pageOrder.reason && (
                                <p className="mt-1 text-ink-muted">{pageOrder.reason}</p>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="mt-2 text-xs text-ink-muted">No page reorder proposed.</p>
                      )}
                    </div>
                    <div className="rounded-lg bg-paper-warm/70 px-3 py-3">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                        Element updates
                      </p>
                      {suggestedElementUpdates.length > 0 ? (
                        <div className="mt-2 space-y-2">
                          {suggestedElementUpdates.map((update) => (
                            <div
                              key={`${task.id}-element-update-${update.review_id}`}
                              className="text-xs text-ink"
                            >
                              <p className="font-medium">
                                {update.review_id} -&gt; {update.new_type}
                                {typeof update.new_level === "number" ? ` (H${update.new_level})` : ""}
                              </p>
                              <p className="mt-1 text-ink-muted">
                                {update.page ? `Page ${update.page}` : "Page unknown"}
                                {update.reason ? ` · ${update.reason}` : ""}
                              </p>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="mt-2 text-xs text-ink-muted">No type or heading-level changes proposed.</p>
                      )}
                    </div>
                  </div>
                  {!canApplyReadingOrder && (
                    <p className="mt-3 text-xs text-warning">
                      This suggestion does not line up cleanly with the current editable structure, so it cannot be applied automatically.
                    </p>
                  )}
                </div>
              )}
              {task.task_type === "table_semantics" && (
                <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-semibold text-ink">
                        Suggested table fixes
                      </p>
                      <p className="mt-1 text-xs text-ink-muted">
                        Review each flagged table independently. Load one table at a time when you want targeted changes, or load all suggested table fixes together.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onApplyTableSuggestion(task, llmSuggestion)}
                      disabled={!canApplyTable || updateStructurePending}
                      className="
                        px-3 py-2 rounded-lg text-xs font-medium
                        bg-accent text-white
                        hover:bg-accent/90 transition-colors
                        disabled:opacity-50 disabled:cursor-not-allowed
                      "
                    >
                      Load All Table Fixes
                    </button>
                  </div>
                  <div className="mt-3 space-y-4">
                    {tableTargets.length > 0 ? (
                      tableTargets.map((target) => {
                        const update = tableHeaderUpdateForTarget(llmSuggestion, target.table_review_id);
                        const previewUrl = tableTargetPreviewUrl(jobId, task.id, target);
                        const canApplyTarget = canApplySingleTableSuggestion(editingStructure, update);
                        return (
                          <div
                            key={`${task.id}-table-target-${target.table_review_id}`}
                            className="rounded-lg border border-ink/8 bg-paper-warm/60 px-3 py-3"
                          >
                            <div className="flex flex-col gap-3 lg:flex-row">
                              <div className="lg:w-56 shrink-0">
                                {previewUrl ? (
                                  <a
                                    href={previewUrl}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="block rounded-lg border border-ink/8 bg-white/70 p-2 no-underline"
                                  >
                                    <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                      Table preview
                                    </p>
                                    <img
                                      src={previewUrl}
                                      alt={`Preview of table ${target.table_review_id ?? "target"}`}
                                      loading="lazy"
                                      className="mt-2 w-full rounded-md border border-ink/6 bg-paper-warm object-cover"
                                    />
                                  </a>
                                ) : (
                                  <div className="rounded-lg border border-ink/8 bg-white/70 p-3">
                                    <p className="text-xs text-ink-muted">Preview unavailable for this table target.</p>
                                  </div>
                                )}
                                {typeof target.page === "number" && (
                                  <a
                                    href={pagePreviewUrl(jobId, target.page)}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="mt-2 inline-flex text-xs font-medium text-accent no-underline hover:underline"
                                  >
                                    Open full page preview
                                  </a>
                                )}
                              </div>
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-start justify-between gap-3">
                                  <div>
                                    <p className="text-sm font-medium text-ink">
                                      {target.table_review_id ?? "Table target"}
                                      {typeof target.page === "number" ? ` · Page ${target.page}` : ""}
                                      {typeof target.num_rows === "number" && typeof target.num_cols === "number"
                                        ? ` · ${target.num_rows}x${target.num_cols}`
                                        : ""}
                                    </p>
                                    {typeof target.risk_score === "number" && (
                                      <p className="mt-1 text-xs text-ink-muted">
                                        Risk score {target.risk_score.toFixed(1)}
                                      </p>
                                    )}
                                  </div>
                                  <button
                                    type="button"
                                    onClick={() => onApplyTableSuggestion(task, llmSuggestion, target.table_review_id)}
                                    disabled={!canApplyTarget || updateStructurePending || !update}
                                    className="
                                      px-3 py-2 rounded-lg text-xs font-medium
                                      bg-accent text-white
                                      hover:bg-accent/90 transition-colors
                                      disabled:opacity-50 disabled:cursor-not-allowed
                                    "
                                    >
                                    Load This Table Fix
                                  </button>
                                </div>

                                {target.risk_reasons && target.risk_reasons.length > 0 && (
                                  <div className="mt-3">
                                    <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                      Why this table is risky
                                    </p>
                                    <div className="mt-2 flex flex-wrap gap-2">
                                      {target.risk_reasons.map((reason, index) => (
                                        <span
                                          key={`${task.id}-${target.table_review_id}-risk-${index}`}
                                          className="rounded-full bg-warning-light px-2 py-1 text-[11px] text-warning"
                                        >
                                          {reason}
                                        </span>
                                      ))}
                                    </div>
                                  </div>
                                )}

                                <div className="mt-3 grid gap-3 md:grid-cols-2">
                                  <div className="rounded-lg bg-white/70 px-3 py-3">
                                    <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                      Current header flags
                                    </p>
                                    <p className="mt-2 text-xs text-ink-muted">
                                      Header rows: {target.header_rows && target.header_rows.length > 0 ? target.header_rows.join(", ") : "none"}
                                    </p>
                                    <p className="mt-1 text-xs text-ink-muted">
                                      Row-header columns: {target.row_header_columns && target.row_header_columns.length > 0 ? target.row_header_columns.join(", ") : "none"}
                                    </p>
                                  </div>
                                  <div className="rounded-lg bg-white/70 px-3 py-3">
                                    <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                      Gemini proposal
                                    </p>
                                    {update ? (
                                      <>
                                        <p className="mt-2 text-xs text-ink-muted">
                                          Header rows: {update.header_rows.length > 0 ? update.header_rows.join(", ") : "none"}
                                        </p>
                                        <p className="mt-1 text-xs text-ink-muted">
                                          Row-header columns: {update.row_header_columns.length > 0 ? update.row_header_columns.join(", ") : "none"}
                                        </p>
                                        {update.reason && (
                                          <p className="mt-2 text-xs text-ink-muted">{update.reason}</p>
                                        )}
                                      </>
                                    ) : (
                                      <p className="mt-2 text-xs text-ink-muted">
                                        No concrete header update was proposed for this table.
                                      </p>
                                    )}
                                  </div>
                                </div>

                                {target.text_excerpt && (
                                  <p className="mt-3 text-xs text-ink-muted">{target.text_excerpt}</p>
                                )}

                                {!canApplyTarget && update && (
                                  <p className="mt-3 text-xs text-warning">
                                    This table proposal does not line up cleanly with the current editable structure.
                                  </p>
                                )}
                              </div>
                            </div>
                          </div>
                        );
                      })
                    ) : suggestedTableUpdates.length > 0 ? (
                      suggestedTableUpdates.map((update) => (
                        <div
                          key={`${task.id}-table-update-${update.table_review_id}`}
                          className="rounded-lg bg-paper-warm/70 px-3 py-3"
                        >
                          <p className="text-sm font-medium text-ink">
                            {update.table_review_id}
                            {typeof update.page === "number" ? ` · Page ${update.page}` : ""}
                          </p>
                          <p className="mt-1 text-xs text-ink-muted">
                            Header rows: {update.header_rows.length > 0 ? update.header_rows.join(", ") : "none"}
                            {" · "}
                            Row-header columns: {update.row_header_columns.length > 0 ? update.row_header_columns.join(", ") : "none"}
                          </p>
                          {update.reason && (
                            <p className="mt-2 text-xs text-ink-muted">{update.reason}</p>
                          )}
                        </div>
                      ))
                    ) : (
                      <p className="text-xs text-ink-muted">No concrete header update was proposed.</p>
                    )}
                  </div>
                  {hasUnsavedStructureEdits && (
                    <div className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-white/70 px-3 py-3">
                      <p className="text-xs text-ink-muted">
                        Suggested table fixes are loaded as unsaved structure changes.
                      </p>
                      <button
                        type="button"
                        onClick={onSaveStructure}
                        disabled={updateStructurePending}
                        className="
                          px-3 py-2 rounded-lg text-xs font-medium
                          bg-accent text-white
                          hover:bg-accent/90 transition-colors
                          disabled:opacity-50 disabled:cursor-not-allowed
                        "
                      >
                        {updateStructurePending ? "Reprocessing..." : "Save Table Changes"}
                      </button>
                    </div>
                  )}
                  {!canApplyTable && llmSuggestion && (
                    <p className="mt-3 text-xs text-warning">
                      This suggestion does not line up cleanly with the current table structure, so it cannot be applied automatically.
                    </p>
                  )}
                </div>
              )}
              {llmSuggestion.review_focus && llmSuggestion.review_focus.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-ink mb-2">
                    Suggested review focus
                  </p>
                  <div className="space-y-2">
                    {llmSuggestion.review_focus.map((item, index) => (
                      <div
                        key={`${task.id}-focus-${index}`}
                        className="rounded-lg bg-white/70 px-3 py-2"
                      >
                        <p className="text-sm text-ink">
                          {item.page ? `Page ${item.page}` : "Page unknown"}
                          {item.font ? ` · ${item.font}` : ""}
                          {item.rule_id ? ` · ${item.rule_id}` : ""}
                        </p>
                        {item.visible_text_hypothesis && (
                          <p className="mt-1 text-xs text-ink-muted">
                            Visible text hypothesis: {item.visible_text_hypothesis}
                          </p>
                        )}
                        {typeof item.is_likely_decorative === "boolean" && (
                          <p className="mt-1 text-xs text-ink-muted">
                            Likely decorative: {item.is_likely_decorative ? "yes" : "no"}
                          </p>
                        )}
                        {item.recommended_reviewer_action && (
                          <p className="mt-1 text-xs text-ink-muted">
                            Reviewer action: {item.recommended_reviewer_action}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {llmSuggestion.reviewer_checklist && llmSuggestion.reviewer_checklist.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-ink mb-2">
                    Suggested checklist
                  </p>
                  <div className="space-y-1">
                    {llmSuggestion.reviewer_checklist.map((item, index) => (
                      <p key={`${task.id}-llm-check-${index}`} className="text-xs text-ink-muted">
                        {index + 1}. {item}
                      </p>
                    ))}
                  </div>
                </div>
              )}
              {suggestionGeneratedAt && (
                <p className="text-[11px] text-ink-muted">
                  Generated {suggestionGeneratedAt}
                </p>
              )}
            </div>
          ) : (
            <p className="mt-3 text-xs text-ink-muted">
              No suggestion generated yet.
            </p>
          )}
          {suggestReviewTaskError && suggestErrorTask === task.id && (
            <p className="mt-3 text-xs text-error">
              {suggestReviewTaskError.message || "Failed to generate suggestion"}
            </p>
          )}
        </div>
      )}

      {/* Evidence fields and resolution note (non-validation tasks only) */}
      {task.source !== "validation" && (
        <>
          {evidenceFields.length > 0 && (
            <div className="mt-4">
              <p className="text-xs font-semibold text-ink mb-2">
                Review evidence
              </p>
              <div className="grid gap-3 md:grid-cols-2">
                {evidenceFields.map((field) => (
                  <label key={`${task.id}-${field.key}`} className="block">
                    <span className="block text-xs font-semibold text-ink mb-1">
                      {field.label}
                    </span>
                    <input
                      type="text"
                      value={evidenceValueForTask(task, field.key)}
                      onChange={(e) =>
                        onEvidenceChange(
                          task.id,
                          existingEvidenceForTask(task),
                          field.key,
                          e.target.value,
                        )
                      }
                      placeholder={field.placeholder}
                      className="
                        w-full rounded-lg border border-ink/10 bg-white/70 px-3 py-2
                        text-sm text-ink placeholder:text-ink-muted/70
                        focus:outline-none focus:ring-2 focus:ring-accent/20
                      "
                    />
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="mt-4">
            <label className="block text-xs font-semibold text-ink mb-2">
              Reviewer note
            </label>
            <textarea
              value={resolutionNote}
              onChange={(e) => onResolutionNoteChange(task.id, e.target.value)}
              rows={3}
              placeholder="Record what you checked and how you verified it."
              className="
                w-full rounded-lg border border-ink/10 bg-white/70 px-3 py-2
                text-sm text-ink placeholder:text-ink-muted/70
                focus:outline-none focus:ring-2 focus:ring-accent/20
              "
            />
          </div>
          {task.status !== "resolved" && !canResolve && (
            <p className="mt-3 text-xs text-warning">
              Required before marking reviewed:
              {resolutionNote.trim().length === 0 ? " reviewer note" : ""}
              {resolutionNote.trim().length === 0 && missingEvidence.length > 0
                ? "; "
                : " "}
              {missingEvidence.length > 0
                ? `${missingEvidence.join(", ")}`
                : ""}
            </p>
          )}
        </>
      )}

      {/* Task action buttons */}
      <div className="mt-4 flex items-center gap-2">
        {task.source === "validation" ? (
          <span className="text-xs text-ink-muted bg-paper-warm px-2 py-1 rounded-full">
            Read-only: requires actual PDF remediation
          </span>
        ) : (
          <button
            type="button"
            onClick={() =>
              onUpdateTask(
                task,
                task.status === "resolved" ? "pending_review" : "resolved",
              )
            }
            disabled={
              savingTask === task.id
              || updateReviewTaskPending
              || (task.status !== "resolved" && !canResolve)
            }
            className="
              px-4 py-2 rounded-lg text-sm font-medium
              bg-accent text-white
              hover:bg-accent/90 transition-colors
              disabled:opacity-50 disabled:cursor-not-allowed
            "
          >
            {savingTask === task.id
              ? "Saving..."
              : task.status === "resolved"
                ? "Reopen Task"
                : "Mark Reviewed"}
          </button>
        )}
        <span className="text-xs text-ink-muted capitalize">
          status: {task.status.replaceAll("_", " ")}
        </span>
        {updateReviewTaskError && savingTask === null && (
          <span className="text-xs text-error">
            {updateReviewTaskError.message || "Failed to update task"}
          </span>
        )}
      </div>
    </div>
  );
}
