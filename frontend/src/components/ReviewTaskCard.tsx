import type { ReviewTask } from "../types";
import {
  applicableActualTextCandidates,
  canAcceptRecommendation,
  documentOverlayForSuggestion,
  LLM_SUGGESTION_TASK_TYPES,
  llmSuggestionForTask,
  pagePreviewUrl,
  previewPagesForTask,
  readingOrderElementUpdates,
  readingOrderPageOrders,
  fontReviewTargets,
  tableReviewTargets,
} from "../pages/reviewHelpers";
import GeminiAssessmentPanel from "./GeminiAssessmentPanel";
import PreviewImage from "./PreviewImage";

export interface MutationStatus {
  isPending: boolean;
  error: Error | null;
  key?: string | number | null;
}

export interface TaskMutationState {
  suggestTask: MutationStatus;
  suggestingTask: number | null;
  suggestErrorTask: number | null;
  acceptingRecommendationTaskId?: number | null;
  acceptRecommendationErrorTaskId?: number | null;
  acceptRecommendationError?: Error | null;
}

interface ReviewTaskCardProps {
  jobId: string;
  task: ReviewTask;
  taskMutation: TaskMutationState;
  onAcceptRecommendation: (task: ReviewTask) => Promise<void> | void;
  onSuggestTask: (task: ReviewTask, feedback?: string) => void;
}

export default function ReviewTaskCard({
  jobId,
  task,
  taskMutation,
  onAcceptRecommendation,
  onSuggestTask,
}: ReviewTaskCardProps) {
  const {
    suggestingTask,
    suggestErrorTask,
    suggestTask: { isPending: suggestReviewTaskPending, error: suggestReviewTaskError },
    acceptingRecommendationTaskId,
    acceptRecommendationErrorTaskId,
    acceptRecommendationError,
  } = taskMutation;

  const llmSuggestion = llmSuggestionForTask(task);
  const documentOverlay = documentOverlayForSuggestion(llmSuggestion);
  const previewPages = previewPagesForTask(task, llmSuggestion);
  const supportsSuggestion = LLM_SUGGESTION_TASK_TYPES.has(task.task_type);
  const suggestionGeneratedAt = llmSuggestion?.generated_at
    ? new Date(llmSuggestion.generated_at).toLocaleString()
    : null;
  const suggestedPageOrders = readingOrderPageOrders(llmSuggestion);
  const suggestedElementUpdates = readingOrderElementUpdates(llmSuggestion);
  const fontTargets = fontReviewTargets(task);
  const tableTargets = tableReviewTargets(task);
  const suggestedActualTextCandidates = applicableActualTextCandidates(llmSuggestion, fontTargets);
  const canAccept = canAcceptRecommendation(task, llmSuggestion);
  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{task.title}</h3>
          <p className="mt-1 text-sm text-ink-light">{task.detail}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          <span className="rounded-full bg-error-light px-2 py-1 text-[11px] font-medium text-error">
            Must Fix
          </span>
          <span className="rounded-full bg-paper-warm px-2 py-1 text-[11px] text-ink-muted">
            {task.source === "validation" ? "Compliance" : "Content"}
          </span>
        </div>
      </div>

      {previewPages.length > 0 && (
        <div className="mt-4">
          {previewPages.slice(0, 1).map((page) => {
            const previewUrl = pagePreviewUrl(jobId, page);
            return (
              <PreviewImage
                key={`${task.id}-preview-${page}`}
                src={previewUrl}
                href={previewUrl}
                alt={`Preview of page ${page}`}
                title={`Page ${page}`}
              />
            );
          })}
        </div>
      )}

      {supportsSuggestion || llmSuggestion ? (
        <GeminiAssessmentPanel
          task={task}
          llmSuggestion={llmSuggestion}
          supportsSuggestion={supportsSuggestion}
          suggestingTask={suggestingTask}
          suggestReviewTaskPending={suggestReviewTaskPending}
          suggestReviewTaskError={suggestReviewTaskError}
          suggestErrorTask={suggestErrorTask}
          documentOverlay={documentOverlay}
          suggestionGeneratedAt={suggestionGeneratedAt}
          suggestedPageOrders={suggestedPageOrders}
          suggestedElementUpdates={suggestedElementUpdates}
          tableTargets={tableTargets}
          suggestedActualTextCandidates={suggestedActualTextCandidates}
          canAcceptRecommendation={canAccept}
          acceptRecommendationPending={acceptingRecommendationTaskId === task.id}
          acceptRecommendationError={
            acceptRecommendationErrorTaskId === task.id ? acceptRecommendationError : null
          }
          onAcceptRecommendation={onAcceptRecommendation}
          onSuggestTask={onSuggestTask}
        />
      ) : (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <p className="text-sm text-ink">This issue does not have a recommendation yet.</p>
          <p className="mt-1 text-xs text-ink-muted">
            The system needs another pass before you can accept or revise a recommendation here.
          </p>
        </div>
      )}
    </div>
  );
}
