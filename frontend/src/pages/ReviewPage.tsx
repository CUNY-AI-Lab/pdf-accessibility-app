import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAppliedChanges,
  useApplyReviewRecommendation,
  useJob,
  useKeepAppliedChange,
  useReviewTasks,
  useSuggestAppliedChange,
  useSuggestReviewTask,
  useUndoAppliedChange,
} from "../api/jobs";
import AppliedChangeCard from "../components/AppliedChangeCard";
import { ChevronLeftIcon } from "../components/Icons";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { TaskMutationState } from "../components/ReviewTaskCard";
import type { AppliedChange, ReviewTask } from "../types";

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const canReviewOutput = job?.status === "complete" || job?.status === "awaiting_recommendation_review";
  const isLoading = false;
  const canReviewTasks = job?.status === "awaiting_recommendation_review";
  const { data: reviewTasks, isLoading: tasksLoading } = useReviewTasks(id!, canReviewTasks);
  const { data: appliedChanges, isLoading: appliedChangesLoading } = useAppliedChanges(id!, canReviewOutput);

  const suggestReviewTask = useSuggestReviewTask(id!);
  const applyReviewRecommendation = useApplyReviewRecommendation(id!);
  const keepAppliedChange = useKeepAppliedChange(id!);
  const undoAppliedChange = useUndoAppliedChange(id!);
  const suggestAppliedChange = useSuggestAppliedChange(id!);

  const [suggestingTask, setSuggestingTask] = useState<number | null>(null);
  const [suggestErrorTask, setSuggestErrorTask] = useState<number | null>(null);
  const [acceptingRecommendationTaskId, setAcceptingRecommendationTaskId] = useState<number | null>(null);
  const [acceptRecommendationErrorTaskId, setAcceptRecommendationErrorTaskId] = useState<number | null>(null);
  const [acceptRecommendationError, setAcceptRecommendationError] = useState<Error | null>(null);
  const [keepingChangeId, setKeepingChangeId] = useState<number | null>(null);
  const [undoingChangeId, setUndoingChangeId] = useState<number | null>(null);
  const [suggestingChangeId, setSuggestingChangeId] = useState<number | null>(null);
  const [changeActionErrorId, setChangeActionErrorId] = useState<number | null>(null);
  const [changeActionError, setChangeActionError] = useState<Error | null>(null);

  const isRecommendationReview = job?.status === "awaiting_recommendation_review";
  const isComplete = job?.status === "complete";

  const handleSuggestTask = async (task: ReviewTask, feedback?: string) => {
    setSuggestingTask(task.id);
    setSuggestErrorTask(null);
    try {
      await suggestReviewTask.mutateAsync({ taskId: task.id, feedback });
    } catch {
      setSuggestErrorTask(task.id);
    } finally {
      setSuggestingTask(null);
    }
  };

  const handleAcceptRecommendation = async (task: ReviewTask) => {
    setAcceptRecommendationErrorTaskId(null);
    setAcceptRecommendationError(null);
    setAcceptingRecommendationTaskId(task.id);
    try {
      await applyReviewRecommendation.mutateAsync({ taskId: task.id });
      navigate(`/jobs/${id}`);
    } catch (error) {
      setAcceptRecommendationErrorTaskId(task.id);
      setAcceptRecommendationError(
        error instanceof Error ? error : new Error("Failed to apply the recommendation"),
      );
    } finally {
      setAcceptingRecommendationTaskId(null);
    }
  };

  const handleKeepAppliedChange = async (change: AppliedChange) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setKeepingChangeId(change.id);
    try {
      await keepAppliedChange.mutateAsync({ changeId: change.id });
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(error instanceof Error ? error : new Error("Failed to keep this change"));
    } finally {
      setKeepingChangeId(null);
    }
  };

  const handleUndoAppliedChange = async (change: AppliedChange) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setUndoingChangeId(change.id);
    try {
      const result = await undoAppliedChange.mutateAsync({ changeId: change.id });
      if (result.job_status === "processing" || result.job_status === "failed") {
        navigate(`/jobs/${id}`);
      }
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(error instanceof Error ? error : new Error("Failed to undo this change"));
    } finally {
      setUndoingChangeId(null);
    }
  };

  const handleSuggestAppliedChange = async (change: AppliedChange, feedback?: string) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setSuggestingChangeId(change.id);
    try {
      await suggestAppliedChange.mutateAsync({ changeId: change.id, feedback });
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(error instanceof Error ? error : new Error("Failed to revise this change"));
    } finally {
      setSuggestingChangeId(null);
    }
  };

  const openReviewTasks = reviewTasks?.filter((task) => task.status === "pending_review") ?? [];
  const blockingTasks = openReviewTasks.filter((task) => task.blocking);
  const pendingAppliedChanges = appliedChanges?.filter((change) => change.review_status === "pending_review") ?? [];

  const sharedTaskMutation: TaskMutationState = {
    suggestTask: {
      isPending: suggestReviewTask.isPending,
      error: suggestReviewTask.isError ? suggestReviewTask.error : null,
    },
    suggestingTask,
    suggestErrorTask,
    acceptingRecommendationTaskId,
    acceptRecommendationErrorTaskId,
    acceptRecommendationError,
  };

  if (isLoading || tasksLoading || appliedChangesLoading) {
    return (
      <div className="max-w-3xl mx-auto animate-pulse-soft">
        <div className="h-6 bg-paper-warm rounded w-48 mb-8" />
        <div className="space-y-4">
          {[1, 2].map((i) => (
            <div key={i} className="h-48 bg-paper-warm rounded-xl" />
          ))}
        </div>
      </div>
    );
  }

  if (isComplete) {
    return (
      <div className="max-w-3xl mx-auto animate-fade-in">
        <Link
          to={`/jobs/${id}`}
          className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink transition-colors no-underline mb-6"
        >
          <ChevronLeftIcon size={14} />
          Back to job
        </Link>
        {pendingAppliedChanges.length > 0 ? (
          <div className="space-y-6">
            <div className="rounded-xl border border-ink/6 bg-cream p-5">
              <h1 className="text-2xl text-ink tracking-tight mb-2">Review important applied changes</h1>
              <p className="text-sm text-ink-muted">
                The app already fixed this PDF. Review the few changes most likely to matter, keep the ones that look right, or describe what should change and the model will revise them.
              </p>
            </div>
            <div className="space-y-4">
              {pendingAppliedChanges.map((change) => (
                <AppliedChangeCard
                  key={change.id}
                  change={change}
                  onKeep={handleKeepAppliedChange}
                  onUndo={handleUndoAppliedChange}
                  onSuggestAlternative={handleSuggestAppliedChange}
                  keeping={keepingChangeId === change.id}
                  undoing={undoingChangeId === change.id}
                  suggesting={suggestingChangeId === change.id}
                  actionError={changeActionErrorId === change.id ? changeActionError : null}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="rounded-xl border border-info/25 bg-info-light/20 p-6">
            <h1 className="text-2xl text-ink tracking-tight mb-2">External QA only</h1>
            <p className="text-sm text-ink-muted">
              This PDF already passed release checks. Download the file and, if needed, test it with a
              screen reader, PAC, or Acrobat.
            </p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto animate-fade-in pb-12">
      <Link
        to={`/jobs/${id}`}
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink transition-colors no-underline mb-6"
      >
        <ChevronLeftIcon size={14} />
        Back to job
      </Link>

      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl md:text-3xl text-ink tracking-tight mb-1">
            Review recommendations
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
          </p>
        </div>
      </div>

      {isRecommendationReview && (
        <div className="mb-8 rounded-xl border border-ink/6 bg-cream p-5">
          <p className="text-sm text-ink-muted">
            The app already fixed most of this PDF. First review the important changes it applied. Then answer any remaining questions by accepting the recommendation or describing what is wrong so the model can revise that part.
          </p>
        </div>
      )}

      {pendingAppliedChanges.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-accent/20 bg-accent-glow/20 p-5">
            <h2 className="text-lg text-ink mb-1">Important changes already applied</h2>
            <p className="text-sm text-ink-muted">
              These fixes are already in the current PDF. Keep them if they look right, undo them, or suggest a better alternative.
            </p>
          </div>
          {pendingAppliedChanges.map((change) => (
            <AppliedChangeCard
              key={change.id}
              change={change}
              onKeep={handleKeepAppliedChange}
              onUndo={handleUndoAppliedChange}
              onSuggestAlternative={handleSuggestAppliedChange}
              keeping={keepingChangeId === change.id}
              undoing={undoingChangeId === change.id}
              suggesting={suggestingChangeId === change.id}
              actionError={changeActionErrorId === change.id ? changeActionError : null}
            />
          ))}
        </section>
      )}

      {isRecommendationReview && reviewTasks && reviewTasks.length > 0 && (
        <div className="space-y-8 mb-8">
          {blockingTasks.length > 0 ? (
            <section>
              <h2 className="text-lg text-ink mb-4">Remaining questions</h2>
              <div className="space-y-4">
                {blockingTasks.map((task) => (
                  <ReviewTaskCard
                    key={task.id}
                    jobId={id!}
                    task={task}
                    taskMutation={sharedTaskMutation}
                    onAcceptRecommendation={handleAcceptRecommendation}
                    onSuggestTask={handleSuggestTask}
                  />
                ))}
              </div>
            </section>
          ) : (
            <div className="text-center py-16 rounded-xl bg-cream border border-ink/6 mb-8">
              <h3 className="font-display text-lg text-ink mb-1">No blocking recommendation issues</h3>
              <p className="text-sm text-ink-muted">
                This job is waiting on recommendation review, but there are no blocking tasks left.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
