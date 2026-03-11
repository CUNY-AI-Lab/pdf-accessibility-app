import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAltTexts,
  useAcceptAltTextRecommendation,
  useApplyReviewRecommendation,
  useJob,
  useReviewTasks,
  useSuggestReviewTask,
  useSuggestAltText,
} from "../api/jobs";
import AltTextRecommendationCard from "../components/AltTextRecommendationCard";
import { ChevronLeftIcon } from "../components/Icons";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { TaskMutationState } from "../components/ReviewTaskCard";
import type { ReviewTask } from "../types";
import { pluralize } from "../utils/format";

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const { data: altTexts, isLoading } = useAltTexts(id!, true);
  const canReviewTasks = job?.status === "awaiting_recommendation_review";
  const { data: reviewTasks, isLoading: tasksLoading } = useReviewTasks(id!, canReviewTasks);

  const acceptAltTextRecommendation = useAcceptAltTextRecommendation(id!);
  const suggestAltText = useSuggestAltText(id!);
  const suggestReviewTask = useSuggestReviewTask(id!);
  const applyReviewRecommendation = useApplyReviewRecommendation(id!);

  const [acceptingFigure, setAcceptingFigure] = useState<number | null>(null);
  const [acceptAltErrorFigure, setAcceptAltErrorFigure] = useState<number | null>(null);
  const [acceptAltError, setAcceptAltError] = useState<Error | null>(null);
  const [suggestingFigure, setSuggestingFigure] = useState<number | null>(null);
  const [suggestAltErrorFigure, setSuggestAltErrorFigure] = useState<number | null>(null);
  const [suggestAltError, setSuggestAltError] = useState<Error | null>(null);
  const [suggestingTask, setSuggestingTask] = useState<number | null>(null);
  const [suggestErrorTask, setSuggestErrorTask] = useState<number | null>(null);
  const [acceptingRecommendationTaskId, setAcceptingRecommendationTaskId] = useState<number | null>(null);
  const [acceptRecommendationErrorTaskId, setAcceptRecommendationErrorTaskId] = useState<number | null>(null);
  const [acceptRecommendationError, setAcceptRecommendationError] = useState<Error | null>(null);

  const isRecommendationReview = job?.status === "awaiting_recommendation_review";
  const isComplete = job?.status === "complete";

  const handleAcceptAltRecommendation = async (figureIndex: number) => {
    setAcceptingFigure(figureIndex);
    setAcceptAltErrorFigure(null);
    setAcceptAltError(null);
    try {
      const result = await acceptAltTextRecommendation.mutateAsync({ figureIndex });
      if (result.job_status !== "awaiting_recommendation_review") {
        navigate(`/jobs/${id}`);
      }
    } catch (error) {
      setAcceptAltErrorFigure(figureIndex);
      setAcceptAltError(error instanceof Error ? error : new Error("Failed to apply the recommendation"));
    } finally {
      setAcceptingFigure(null);
    }
  };

  const handleSuggestAltRecommendation = async (figureIndex: number, feedback?: string) => {
    setSuggestingFigure(figureIndex);
    setSuggestAltErrorFigure(null);
    setSuggestAltError(null);
    try {
      await suggestAltText.mutateAsync({ figureIndex, feedback });
    } catch (error) {
      setSuggestAltErrorFigure(figureIndex);
      setSuggestAltError(error instanceof Error ? error : new Error("Failed to revise the recommendation"));
    } finally {
      setSuggestingFigure(null);
    }
  };

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

  const pendingAltTexts = altTexts?.filter((a) => a.status === "pending_review") ?? [];
  const openReviewTasks = reviewTasks?.filter((task) => task.status === "pending_review") ?? [];
  const blockingTasks = openReviewTasks.filter((task) => task.blocking);

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

  if (isLoading || tasksLoading) {
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
        <div className="rounded-xl border border-info/25 bg-info-light/20 p-6">
          <h1 className="text-2xl text-ink tracking-tight mb-2">External QA only</h1>
          <p className="text-sm text-ink-muted">
            This PDF already passed release checks. Download the file and, if needed, test it with a
            screen reader, PAC, or Acrobat.
          </p>
        </div>
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
            {isRecommendationReview && altTexts && pendingAltTexts.length > 0 && (
              <span>
                {" "}&middot; {pendingAltTexts.length} {pluralize(pendingAltTexts.length, "figure")}
              </span>
            )}
          </p>
        </div>
      </div>

      {isRecommendationReview && (
        <div className="mb-8 rounded-xl border border-ink/6 bg-cream p-5">
          <p className="text-sm text-ink-muted">
            The model has already analyzed the hard parts of this PDF. For each remaining question, accept the recommendation when it is ready or describe what is wrong so the model can revise that part.
          </p>
        </div>
      )}

      {isRecommendationReview && pendingAltTexts.length > 0 && (
        <div className="space-y-6 mb-8">
          <div className="rounded-xl border border-ink/6 bg-cream p-5">
            <p className="text-sm text-ink-muted">
              The model found figure descriptions that still need your judgment. Accept the recommendation when it looks right, or describe what should change and the model will revise it.
            </p>
          </div>
          {pendingAltTexts.map((altText) => (
            <AltTextRecommendationCard
              key={altText.id}
              altText={altText}
              onAccept={handleAcceptAltRecommendation}
              onSuggestAlternative={handleSuggestAltRecommendation}
              accepting={acceptingFigure === altText.figure_index}
              acceptError={
                acceptAltErrorFigure === altText.figure_index ? acceptAltError : null
              }
              suggesting={suggestingFigure === altText.figure_index}
              suggestError={
                suggestAltErrorFigure === altText.figure_index ? suggestAltError : null
              }
            />
          ))}
        </div>
      )}

      {isRecommendationReview && reviewTasks && reviewTasks.length > 0 && (
        <div className="space-y-8 mb-8">
          {blockingTasks.length > 0 ? (
            <section>
              <h2 className="text-lg text-ink mb-4">Questions for you</h2>
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
