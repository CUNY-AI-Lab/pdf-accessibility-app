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

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const canReviewOutput = job?.status === "complete" || job?.status === "manual_remediation";
  const { data: reviewTasks, isLoading: tasksLoading } = useReviewTasks(id!, canReviewOutput);
  const { data: appliedChanges, isLoading: appliedChangesLoading } = useAppliedChanges(id!, canReviewOutput);
  const keepAppliedChange = useKeepAppliedChange(id!);
  const undoAppliedChange = useUndoAppliedChange(id!);
  const reviseAppliedChange = useReviseAppliedChange(id!);

  const [keepingChangeId, setKeepingChangeId] = useState<number | null>(null);
  const [undoingChangeId, setUndoingChangeId] = useState<number | null>(null);
  const [revisingChangeId, setRevisingChangeId] = useState<number | null>(null);
  const [changeActionErrorId, setChangeActionErrorId] = useState<number | null>(null);
  const [changeActionError, setChangeActionError] = useState<Error | null>(null);

  const isLoading = !job || tasksLoading || appliedChangesLoading;
  const isManualRemediation = job?.status === "manual_remediation";
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

  const handleReviseAppliedChange = async (change: AppliedChange, feedback?: string) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setRevisingChangeId(change.id);
    try {
      await reviseAppliedChange.mutateAsync({ changeId: change.id, feedback });
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(error instanceof Error ? error : new Error("Failed to revise this change"));
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
            Review output
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
          </p>
        </div>
      </div>

      <div className="mb-8 rounded-xl border border-ink/6 bg-cream p-5">
        <p className="text-sm text-ink-muted">
          {isManualRemediation
            ? "This run stopped short of a trustworthy accessible output. Use the report for manual remediation. Any cards below are optional visible context from the current PDF."
            : hasReviewItems
              ? "The accessible PDF is ready. You can review the visible changes the app made and any follow-up items it flagged."
              : "The accessible PDF is ready. This page is only needed when you want an extra review pass."}
        </p>
      </div>

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
              {isManualRemediation ? "Visible changes in the current output" : "Important changes already applied"}
            </h2>
            <p className="text-sm text-ink-muted">
              {isManualRemediation
                ? "These changes may help you inspect what the app did, but they do not replace manual remediation."
                : "These fixes are already in the current PDF. Keep them if they look right, undo them, or revise them."}
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
                ? "These checks may help you inspect the current output, but they do not replace manual remediation."
                : "These are optional advanced checks if you want to spot-check the final output."}
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

      {!hasReviewItems && !isManualRemediation && (
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
