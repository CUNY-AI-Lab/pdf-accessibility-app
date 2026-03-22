import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiUrl } from "../api/client";
import {
  useAppliedChanges,
  useEditAppliedChange,
  useFigureChanges,
  useJob,
  useKeepAppliedChange,
  useReviseAppliedChange,
  useReviewTasks,
  useUndoAppliedChange,
} from "../api/jobs";
import AppliedChangeCard from "../components/AppliedChangeCard";
import FidelityIssueCard from "../components/FidelityIssueCard";
import { ChevronLeftIcon } from "../components/Icons";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { AppliedChange } from "../types";

const FIDELITY_TASK_TYPES = new Set([
  "content_fidelity",
  "reading_order",
  "table_semantics",
  "form_semantics",
  "font_text_fidelity",
]);

function actionSubject(change: AppliedChange): string {
  return change.change_type === "figure_semantics" ? "image description" : "change";
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
  const {
    data: figureChanges,
    isLoading: figureChangesLoading,
    error: figureChangesError,
  } = useFigureChanges(id!, canInspectOutput);
  const keepAppliedChange = useKeepAppliedChange(id!);
  const undoAppliedChange = useUndoAppliedChange(id!);
  const reviseAppliedChange = useReviseAppliedChange(id!);
  const editAppliedChange = useEditAppliedChange(id!);

  const [keepingChangeId, setKeepingChangeId] = useState<number | null>(null);
  const [undoingChangeId, setUndoingChangeId] = useState<number | null>(null);
  const [revisingChangeId, setRevisingChangeId] = useState<number | null>(null);
  const [editingChangeId, setEditingChangeId] = useState<number | null>(null);
  const [changeActionErrorId, setChangeActionErrorId] = useState<number | null>(null);
  const [changeActionError, setChangeActionError] = useState<Error | null>(null);

  const isLoading = jobLoading || (canInspectOutput && (tasksLoading || appliedChangesLoading || figureChangesLoading));
  const isManualRemediation = job?.status === "manual_remediation";
  const reviewContextError = reviewTasksError || appliedChangesError || figureChangesError;
  const pendingAppliedChanges = appliedChanges?.filter((change) => change.review_status === "pending_review") ?? [];
  const pendingIds = new Set(pendingAppliedChanges.map((c) => c.id));
  const keptFigureChanges = figureChanges?.filter((c) => !pendingIds.has(c.id) && c.review_status === "kept") ?? [];
  const hasFigureCards = pendingAppliedChanges.some((c) => c.change_type === "figure_semantics") || keptFigureChanges.length > 0;
  const allOpenTasks = reviewTasks?.filter((task) => task.status === "pending_review") ?? [];
  const blockingFidelityTasks = allOpenTasks.filter(
    (task) => task.blocking && FIDELITY_TASK_TYPES.has(task.task_type),
  );
  const nonBlockingFidelityTasks = allOpenTasks.filter(
    (task) => !task.blocking && FIDELITY_TASK_TYPES.has(task.task_type),
  );
  const optionalReviewTasks = allOpenTasks
    .filter((task) => !FIDELITY_TASK_TYPES.has(task.task_type))
    .filter((task) => !(task.task_type === "alt_text" && hasFigureCards));
  const additionalCheckTasks = [...nonBlockingFidelityTasks, ...optionalReviewTasks];
  const hasReviewItems = pendingAppliedChanges.length > 0 || allOpenTasks.length > 0;

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
                ? "Failed to retry this image description"
                : `Failed to revise this ${actionSubject(change)}`,
            ),
      );
    } finally {
      setRevisingChangeId(null);
    }
  };

  const handleEditAppliedChange = async (change: AppliedChange, text: string) => {
    setChangeActionErrorId(null);
    setChangeActionError(null);
    setEditingChangeId(change.id);
    try {
      const result = await editAppliedChange.mutateAsync({ changeId: change.id, text });
      if (result.job_status === "processing" || result.job_status === "failed") {
        navigate(`/jobs/${id}`);
      }
    } catch (error) {
      setChangeActionErrorId(change.id);
      setChangeActionError(
        error instanceof Error ? error : new Error("Failed to save the edited description"),
      );
      throw error; // Re-throw so AppliedChangeCard keeps the edit form open
    } finally {
      setEditingChangeId(null);
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
          Review is available once processing completes.
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
            Review
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
          </p>
        </div>
      </div>

      {reviewContextError && (
        <div className="mb-8 rounded-xl border border-warning/30 bg-warning-light/20 p-5">
          <p className="text-sm text-ink-muted">
            Could not load review details. Download links still reflect the current output.
          </p>
        </div>
      )}

      {blockingFidelityTasks.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-error/20 bg-error-light/10 p-6">
            <h2 className="text-lg text-ink mb-1">
              Issues requiring external tools
            </h2>
            <p className="text-sm text-ink-muted">
              {blockingFidelityTasks.length === 1
                ? "1 issue was detected that cannot be fixed within this app."
                : `${blockingFidelityTasks.length} issues were detected that cannot be fixed within this app.`}
              {" "}Use a tool like Adobe Acrobat Pro to address {blockingFidelityTasks.length === 1 ? "it" : "them"}.
            </p>
            <div className="mt-4 flex flex-wrap items-center gap-4">
              <a
                href={apiUrl(`/jobs/${id}/download`)}
                download={job ? `accessible_${job.original_filename}` : undefined}
                className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white no-underline hover:bg-accent/90"
              >
                Download Accessible PDF
              </a>
              <a
                href={apiUrl(`/jobs/${id}/download/report`)}
                download={job ? `report_${job.original_filename}.json` : undefined}
                className="text-sm text-accent font-medium no-underline hover:underline"
              >
                Download report
              </a>
            </div>
          </div>
          {blockingFidelityTasks.map((task) => (
            <FidelityIssueCard key={task.id} jobId={id!} task={task} />
          ))}
        </section>
      )}

      {isManualRemediation && blockingFidelityTasks.length === 0 && (
        <div className="mb-8 rounded-xl border border-warning/30 bg-warning-light/30 p-6">
          <h2 className="text-lg text-ink mb-1">
            Needs manual fixes
          </h2>
          <p className="text-sm text-ink-muted">
            Some issues could not be fixed automatically. Download the report and current PDF to continue in an external tool.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <a
              href={apiUrl(`/jobs/${id}/download`)}
              download={job ? `accessible_${job.original_filename}` : undefined}
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white no-underline hover:bg-accent/90"
            >
              Download Accessible PDF
            </a>
            <a
              href={apiUrl(`/jobs/${id}/download/report`)}
              download={job ? `report_${job.original_filename}.json` : undefined}
              className="text-sm text-accent font-medium no-underline hover:underline"
            >
              Download report
            </a>
          </div>
        </div>
      )}

      {pendingAppliedChanges.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-accent/20 bg-accent-glow/20 p-5">
            <h2 className="text-lg text-ink mb-1">
              Image descriptions
            </h2>
            <p className="text-sm text-ink-muted">
              Review image descriptions the app generated. Keep, undo, or revise each one.
            </p>
          </div>
          {pendingAppliedChanges.map((change) => (
            <AppliedChangeCard
              key={change.id}
              jobId={id!}
              change={change}
              onKeep={handleKeepAppliedChange}
              onUndo={handleUndoAppliedChange}
              onRevise={handleReviseAppliedChange}
              onEdit={handleEditAppliedChange}
              keeping={keepingChangeId === change.id}
              undoing={undoingChangeId === change.id}
              revising={revisingChangeId === change.id}
              editing={editingChangeId === change.id}
              actionError={changeActionErrorId === change.id ? changeActionError : null}
            />
          ))}
        </section>
      )}

      {keptFigureChanges.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-ink/6 bg-cream p-5">
            <h2 className="text-lg text-ink mb-1">
              Image descriptions
            </h2>
            <p className="text-sm text-ink-muted">
              Review and edit the alt text applied to each figure.
            </p>
          </div>
          {keptFigureChanges.map((change) => (
            <AppliedChangeCard
              key={change.id}
              jobId={id!}
              change={change}
              onKeep={handleKeepAppliedChange}
              onUndo={handleUndoAppliedChange}
              onRevise={handleReviseAppliedChange}
              onEdit={handleEditAppliedChange}
              keeping={keepingChangeId === change.id}
              undoing={undoingChangeId === change.id}
              revising={revisingChangeId === change.id}
              editing={editingChangeId === change.id}
              actionError={changeActionErrorId === change.id ? changeActionError : null}
            />
          ))}
        </section>
      )}

      {additionalCheckTasks.length > 0 && (
        <section className="space-y-4 mb-8">
          <div className="rounded-xl border border-ink/6 bg-cream p-5">
            <h2 className="text-lg text-ink mb-1">Additional checks</h2>
            <p className="text-sm text-ink-muted">
              Optional checks for extra confidence in the output.
            </p>
          </div>
          <div className="space-y-4">
            {additionalCheckTasks.map((task) =>
              FIDELITY_TASK_TYPES.has(task.task_type) ? (
                <FidelityIssueCard key={task.id} jobId={id!} task={task} />
              ) : (
                <ReviewTaskCard key={task.id} jobId={id!} task={task} />
              ),
            )}
          </div>
        </section>
      )}

      {!hasReviewItems && !isManualRemediation && !reviewContextError && (
        <div className="rounded-xl border border-success/25 bg-success-light/20 p-6">
          <h2 className="text-2xl text-ink tracking-tight mb-2">
            Nothing to review
          </h2>
          <p className="text-sm text-ink-muted">
            This PDF passed all checks. For extra assurance, test with a screen reader or PAC.
          </p>
          <a
            href={apiUrl(`/jobs/${id}/download/report`)}
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
