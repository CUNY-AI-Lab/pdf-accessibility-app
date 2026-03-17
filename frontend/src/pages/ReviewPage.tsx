import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAppliedChanges,
  useJob,
  useKeepAppliedChange,
  useReviseAppliedChange,
  useReviewTasks,
  useUndoAppliedChange,
} from "../api/jobs";
import AppliedChangeCard from "../components/AppliedChangeCard";
import { ChevronLeftIcon } from "../components/Icons";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { AppliedChange } from "../types";

function actionSubject(change: AppliedChange): string {
  return change.change_type === "figure_semantics" ? "figure decision" : "change";
}

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const {
    data: job,
    isLoading: jobLoading,
    error: jobError,
  } = useJob(id!);
  const canInspectOutput = job?.status === "complete" || job?.status === "manual_remediation";
  const {
    data: reviewTasks,
    isLoading: tasksLoading,
    error: reviewTasksError,
  } = useReviewTasks(id!, canInspectOutput);
  const {
    data: appliedChanges,
    isLoading: appliedChangesLoading,
    error: appliedChangesError,
  } = useAppliedChanges(id!, canInspectOutput);
  const keepAppliedChange = useKeepAppliedChange(id!);
  const undoAppliedChange = useUndoAppliedChange(id!);
  const reviseAppliedChange = useReviseAppliedChange(id!);

  const [keepingChangeId, setKeepingChangeId] = useState<number | null>(null);
  const [undoingChangeId, setUndoingChangeId] = useState<number | null>(null);
  const [revisingChangeId, setRevisingChangeId] = useState<number | null>(null);
  const [changeActionErrorId, setChangeActionErrorId] = useState<number | null>(null);
  const [changeActionError, setChangeActionError] = useState<Error | null>(null);

  const isLoading = jobLoading || (canInspectOutput && (tasksLoading || appliedChangesLoading));
  const isManualRemediation = job?.status === "manual_remediation";
  const reviewContextError = reviewTasksError || appliedChangesError;
  const openReviewTasks = reviewTasks?.filter((task) => task.status === "pending_review") ?? [];
  const pendingAppliedChanges = appliedChanges?.filter((change) => change.review_status === "pending_review") ?? [];
  const hasReviewItems = pendingAppliedChanges.length > 0 || openReviewTasks.length > 0;

  const handleKeepAppliedChange = async (change: AppliedChange) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setKeepingChangeId(change.id);
    try {
      await keepAppliedChange.mutateAsync({ changeId: change.id });
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(
        error instanceof Error ? error : new Error(`Failed to keep this ${actionSubject(change)}`),
      );
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
      setChangeActionError(
        error instanceof Error ? error : new Error(`Failed to undo this ${actionSubject(change)}`),
      );
    } finally {
      setUndoingChangeId(null);
    }
  };

  const handleReviseAppliedChange = async (change: AppliedChange, feedback?: string) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setRevisingChangeId(change.id);
    try {
      await reviseAppliedChange.mutateAsync({ changeId: change.id, feedback });
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(
        error instanceof Error
          ? error
          : new Error(
              change.change_type === "figure_semantics"
                ? "Failed to retry this figure decision"
                : `Failed to revise this ${actionSubject(change)}`,
            ),
      );
    } finally {
      setRevisingChangeId(null);
    }
  };

  if (isLoading) {
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

  if (jobError || !job) {
    return (
      <div className="text-center py-20">
        <h2 className="text-xl font-display text-ink mb-2">Review unavailable</h2>
        <p className="text-sm text-ink-muted mb-6">
          This job may have expired, been deleted, or never existed.
        </p>
        <Link
          to="/dashboard"
          className="text-sm text-accent font-medium no-underline hover:underline"
        >
          &larr; Back to dashboard
        </Link>
      </div>
    );
  }

  if (!canInspectOutput) {
    return (
      <div className="text-center py-20">
        <h2 className="text-xl font-display text-ink mb-2">Review not ready</h2>
        <p className="text-sm text-ink-muted mb-6">
          In-app review is only available after processing reaches a terminal state.
        </p>
        <Link
          to={`/jobs/${id}`}
          className="text-sm text-accent font-medium no-underline hover:underline"
        >
          &larr; Back to job
        </Link>
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
            {isManualRemediation ? "Inspect QA Context" : "Inspect QA Details"}
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
          </p>
        </div>
      </div>

      <div className="mb-8 rounded-xl border border-ink/6 bg-cream p-5">
        <p className="text-sm text-ink-muted">
          {reviewContextError
            ? "We could not load the limited in-app QA context right now. Download links below still reflect the current output."
            : isManualRemediation
            ? "This run stopped short of a trustworthy accessible output. The in-app surface only shows figure decisions and a few visible QA checks from the current PDF. It does not expose or resolve the full blocker set."
            : hasReviewItems
              ? "This PDF already passed release checks. This page only exposes figure decisions the app already made and a few visible QA checks."
              : "This PDF already passed release checks. This page is only useful if you want to inspect the limited in-app QA details."}
        </p>
      </div>

      {reviewContextError && (
        <div className="mb-8 rounded-xl border border-warning/30 bg-warning-light/20 p-5">
          <h2 className="text-lg text-ink mb-1">Visible review details unavailable</h2>
          <p className="text-sm text-ink-muted">
            Reload the page if you want to inspect figure decisions or visible QA checks. The job status and
            download links remain accurate.
          </p>
        </div>
      )}

      {isManualRemediation && (
        <div className="mb-8 rounded-xl border border-warning/30 bg-warning-light/30 p-6">
          <h2 className="text-2xl text-ink tracking-tight mb-2">
            Manual remediation required
          </h2>
          <p className="text-sm text-ink-muted">
            The app could not finish a trustworthy accessible output automatically. Use the validation report and current PDF for manual follow-up outside the app.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <a
              href={`/api/jobs/${id}/download/report`}
              download={job ? `report_${job.original_filename}.json` : undefined}
              className="text-sm text-accent font-medium no-underline hover:underline"
            >
              Download report
            </a>
            <a
              href={`/api/jobs/${id}/download`}
              download={job ? `accessible_${job.original_filename}` : undefined}
              className="text-sm text-accent font-medium no-underline hover:underline"
            >
              Download current PDF
            </a>
          </div>
        </div>
      )}

      {pendingAppliedChanges.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-accent/20 bg-accent-glow/20 p-5">
            <h2 className="text-lg text-ink mb-1">
              {isManualRemediation ? "Figure decisions in the current PDF" : "Figure decisions already applied"}
            </h2>
            <p className="text-sm text-ink-muted">
              {isManualRemediation
                ? "These are figure-specific decisions the app already made. They may help you inspect what happened, but they will not resolve non-figure blockers elsewhere in the document."
                : "These are figure-specific decisions already written into the current PDF. Keep one if it looks right, undo it to remove that figure decision and rerun processing, or retry that figure with more guidance."}
            </p>
          </div>
          {pendingAppliedChanges.map((change) => (
            <AppliedChangeCard
              key={change.id}
              change={change}
              onKeep={handleKeepAppliedChange}
              onUndo={handleUndoAppliedChange}
              onRevise={handleReviseAppliedChange}
              keeping={keepingChangeId === change.id}
              undoing={undoingChangeId === change.id}
              revising={revisingChangeId === change.id}
              actionError={changeActionErrorId === change.id ? changeActionError : null}
            />
          ))}
        </section>
      )}

      {openReviewTasks.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-ink/6 bg-cream p-5">
            <h2 className="text-lg text-ink mb-1">Optional visible checks</h2>
            <p className="text-sm text-ink-muted">
              {isManualRemediation
                ? "These are the only visible QA checks the app exposes in-app. They may help you inspect the current output, but they do not replace manual remediation."
                : "These are the only extra visible QA checks the app exposes in-app today."}
            </p>
          </div>
          <div className="space-y-4">
            {openReviewTasks.map((task) => (
              <ReviewTaskCard
                key={task.id}
                jobId={id!}
                task={task}
              />
            ))}
          </div>
        </section>
      )}

      {!hasReviewItems && !isManualRemediation && !reviewContextError && (
        <div className="rounded-xl border border-info/25 bg-info-light/20 p-6">
          <h2 className="text-2xl text-ink tracking-tight mb-2">
            External QA only
          </h2>
          <p className="text-sm text-ink-muted">
            This PDF already passed release checks. Download the file and, if needed, test it with a screen reader, PAC, or Acrobat.
          </p>
          <a
            href={`/api/jobs/${id}/download/report`}
            download={job ? `report_${job.original_filename}.json` : undefined}
            className="inline-flex mt-4 text-sm text-accent font-medium no-underline hover:underline"
          >
            Download report
          </a>
        </div>
      )}
    </div>
  );
}
