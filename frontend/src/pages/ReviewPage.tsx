import { editorTransition, type EditorState } from "../state/editor";
import { showToastGlobal } from "../hooks/useToast";
import { useMachine } from "../hooks/useMachine";
import { actionLeavesReview, reviewActionTransition, type ReviewAction, type ReviewOperation } from "../state/reviewAction";
import { downloadCapabilities, jobRevision, outputReady, projectJobView } from "../state/jobView";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiUrl } from "../api/client";
import {
  useEditAppliedChange,
  useJob,
  useReviewContext,
  useKeepAppliedChange,
  useResolveReviewTask,
  useReviseAppliedChange,
  useUndoAppliedChange,
} from "../api/jobs";
import AppliedChangeCard from "../components/AppliedChangeCard";
import DownloadButton from "../components/DownloadButton";
import FidelityIssueCard from "../components/FidelityIssueCard";
import { ChevronLeftIcon } from "../components/Icons";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { AppliedChange, ValidationViolation } from "../types";

const FIDELITY_TASK_TYPES = new Set([
  "content_fidelity",
  "reading_order",
  "table_semantics",
  "form_semantics",
  "font_text_fidelity",
]);

const CATEGORY_LABELS: Record<string, string> = {
  fonts: "Font issues",
  structure: "Document structure",
  metadata: "Document metadata",
  images: "Image accessibility",
  color: "Color contrast",
  language: "Language tagging",
  annotations: "Annotations",
};

function categoryLabel(category: string | undefined): string {
  return (category && CATEGORY_LABELS[category]) ?? "Accessibility issue";
}

/** Plain-language descriptions for common fix_hint patterns. */
function plainFixDescription(violation: ValidationViolation): string {
  if (violation.fix_hint) return violation.fix_hint;
  return violation.description;
}

function UnremediatedViolations({ violations }: { violations: ValidationViolation[] }) {
  if (violations.length === 0) return null;

  // Group by category for cleaner display
  const byCategory = new Map<string, ValidationViolation[]>();
  for (const v of violations) {
    const cat = v.category ?? "other";
    const group = byCategory.get(cat) ?? [];
    group.push(v);
    byCategory.set(cat, group);
  }

  const totalCount = violations.reduce((sum, v) => sum + v.count, 0);

  return (
    <div className="mt-4 space-y-3">
      <h3 className="text-sm font-semibold text-ink">
        {totalCount} remaining {totalCount === 1 ? "error" : "errors"} found by the validator
      </h3>
      {[...byCategory.entries()].map(([cat, items]) => (
        <div key={cat} className="rounded-lg border border-ink/8 bg-white/70 px-4 py-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-1">
            {categoryLabel(cat)}
          </h4>
          <ul className="space-y-2">
            {items.map((v) => (
              <li key={v.rule_id} className="text-sm text-ink">
                <p>{plainFixDescription(v)}</p>
                <p className="text-xs text-ink-muted mt-0.5">
                  {v.count} {v.count === 1 ? "occurrence" : "occurrences"}
                  {v.location ? ` \u00b7 ${v.location}` : ""}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  return <ReviewJob key={id} id={id!} />;
}

function ReviewJob({ id }: { id: string }) {
  const navigate = useNavigate();
  const {
    data: job,
    isLoading: jobLoading,
    error: jobError,
  } = useJob(id!);
  const canInspectOutput = outputReady(job);
  const capabilities = downloadCapabilities(job);
  const view = projectJobView(job, jobLoading, jobError);
  const context = useReviewContext(job);
  const reviewTasks = context.status === "ready" ? context.data.tasks : undefined;
  const appliedChanges = context.status === "ready" ? context.data.changes : undefined;
  const figureChanges = context.status === "ready" ? context.data.figures : undefined;
  const validationReport = context.status === "ready" ? context.data.validation : undefined;
  const keepAppliedChange = useKeepAppliedChange(id!);
  const undoAppliedChange = useUndoAppliedChange(id!);
  const reviseAppliedChange = useReviseAppliedChange(id!);
  const editAppliedChange = useEditAppliedChange(id!);
  const resolveReviewTask = useResolveReviewTask(id!);

  const actions = useMachine<ReviewAction, Parameters<typeof reviewActionTransition>[1]>({ status: "idle" }, reviewActionTransition);
  const editor = useMachine<EditorState, Parameters<typeof editorTransition>[1]>({ status: "viewing" }, editorTransition);
  const draftOwner = editor.state.status === "viewing" ? null : editor.state.target;
  const busy = actions.state.status === "pending";
  const isLoading = view.status === "loading" || context.status === "loading";
  const isManualRemediation = job?.status === "manual_remediation";
  const reviewContextError = context.status === "unavailable" ? context.error : null;
  const busyId = (operation: ReviewOperation) => actions.state.status === "pending" && actions.state.operation === operation ? actions.state.target : null;
  const keepingChangeId = busyId("keep");
  const undoingChangeId = busyId("undo");
  const revisingChangeId = busyId("revise");
  const editingChangeId = busyId("edit");
  const resolvingTaskId = busyId("resolve");
  const changeActionErrorId = actions.state.status === "failed" && actions.state.operation !== "resolve" ? actions.state.target : null;
  const changeActionError = actions.state.status === "failed" ? actions.state.error : null;
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
  const unremediatedViolations = validationReport?.violations?.filter(
    (v) => v.remediation_status === "needs_remediation" && v.severity === "error",
  ) ?? [];
  const hasReviewItems = pendingAppliedChanges.length > 0 || allOpenTasks.length > 0 || unremediatedViolations.length > 0;

  const runAction = async (
    operation: ReviewOperation, target: number, permitted: boolean,
    request: () => Promise<{ job_status: string }>,
  ) => {
    const requestId = crypto.randomUUID();
    const draft = editor.read();
    const owner = draft.status === "viewing" ? null : draft.target;
    try {
      actions.send({ type: "start", requestId, operation, target, revision: job ? jobRevision(job) : "", permitted: permitted && context.status === "ready" && (owner === null || (operation === "edit" && owner === target)) });
    } catch (error) {
      if (operation === "edit") throw error;
      showToastGlobal(error instanceof Error ? error.message : "Review action unavailable");
      return;
    }
    let result: { job_status: string };
    try {
      result = await request();
    } catch (error) {
      actions.send({ type: "failed", requestId, error: error instanceof Error ? error : new Error("Review action failed") });
      if (operation === "edit") throw error;
      return;
    }
    const completed = actions.send({ type: "done", requestId });
    if (completed.status === "done" && completed.requestId === requestId && actionLeavesReview(result.job_status)) navigate(`/jobs/${id}`);
  };
  const handleKeepAppliedChange = (change: AppliedChange) => runAction("keep", change.id, change.reviewable, () => keepAppliedChange.mutateAsync({ changeId: change.id }));
  const handleUndoAppliedChange = (change: AppliedChange) => runAction("undo", change.id, change.reviewable, () => undoAppliedChange.mutateAsync({ changeId: change.id }));
  const handleReviseAppliedChange = (change: AppliedChange, feedback?: string) => runAction("revise", change.id, change.reviewable, () => reviseAppliedChange.mutateAsync({ changeId: change.id, feedback }));
  const handleEditAppliedChange = (change: AppliedChange, text: string) => runAction("edit", change.id, change.reviewable, () => editAppliedChange.mutateAsync({ changeId: change.id, text }));
  const handleResolveTask = (taskId: number) => runAction("resolve", taskId, canInspectOutput, () => resolveReviewTask.mutateAsync({ taskId }));

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

  if (view.status === "unavailable" || !job) {
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

      {editor.state.status !== "viewing" && <div className="mb-4 rounded-lg border border-ink/10 p-3 text-sm">
        A description draft is open. Finish or cancel it before another review action.
        <button type="button" className="ml-3 text-accent" disabled={editor.state.status === "saving"} onClick={() => editor.send({ type: "cancel" })}>Cancel draft</button>
      </div>}
      {actions.state.status === "failed" && actions.state.operation === "resolve" && <p role="alert" className="mb-4 text-error">{actions.state.error.message}</p>}
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
              Issues to review
            </h2>
            <p className="text-sm text-ink-muted">
              {blockingFidelityTasks.length === 1
                ? "1 issue needs your attention."
                : `${blockingFidelityTasks.length} issues need your attention.`}
              {" "}Review each one below and either resolve it or fix it in an external tool like Adobe Acrobat Pro.
            </p>
            <div className="mt-4 flex flex-wrap items-center gap-4">
              <a
                href={apiUrl(`/jobs/${id}/download`)}
                download={job ? `accessible_${job.original_filename}` : undefined}
                className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white no-underline hover:bg-accent/90"
              >
                Download Accessible PDF
              </a>
              {capabilities.html && <DownloadButton jobId={job.id} filename={job.original_filename} type="html" />}
              <a
                href={apiUrl(`/jobs/${id}/download/report.html`)}
                download={job ? `report_${job.original_filename.replace(/\.pdf$/i, "")}.html` : undefined}
                className="text-sm text-accent font-medium no-underline hover:underline"
              >
                Download report
              </a>
            </div>
            <UnremediatedViolations violations={unremediatedViolations} />
          </div>
          {blockingFidelityTasks.map((task) => (
            <FidelityIssueCard
              key={task.id}
              jobId={id!}
              task={task}
              onResolve={handleResolveTask}
              resolving={resolvingTaskId === task.id}
              disabled={busy || draftOwner !== null}
            />
          ))}
        </section>
      )}

      {isManualRemediation && blockingFidelityTasks.length === 0 && (
        <div className="mb-8 rounded-xl border border-warning/30 bg-warning-light/30 p-6">
          <h2 className="text-lg text-ink mb-1">
            Needs manual fixes
          </h2>
          <p className="text-sm text-ink-muted">
            {unremediatedViolations.length > 0
              ? "The validator found issues that could not be fixed automatically. Download the PDF and fix them in an external tool like Adobe Acrobat Pro."
              : "Some issues could not be fixed automatically. Download the report and current PDF to continue in an external tool."}
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <a
              href={apiUrl(`/jobs/${id}/download`)}
              download={job ? `accessible_${job.original_filename}` : undefined}
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white no-underline hover:bg-accent/90"
            >
              Download Accessible PDF
            </a>
            {capabilities.html && <DownloadButton jobId={job.id} filename={job.original_filename} type="html" />}
            <a
              href={apiUrl(`/jobs/${id}/download/report.html`)}
              download={job ? `report_${job.original_filename.replace(/\.pdf$/i, "")}.html` : undefined}
              className="text-sm text-accent font-medium no-underline hover:underline"
            >
              Download report
            </a>
          </div>
          <UnremediatedViolations violations={unremediatedViolations} />
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
              revision={jobRevision(job)}
              disabled={busy || (draftOwner !== null && draftOwner !== change.id)}
              editor={editor}
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
              revision={jobRevision(job)}
              disabled={busy || (draftOwner !== null && draftOwner !== change.id)}
              editor={editor}
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
                <FidelityIssueCard
                  key={task.id}
                  jobId={id!}
                  task={task}
                  onResolve={handleResolveTask}
                  resolving={resolvingTaskId === task.id}
                  disabled={busy || draftOwner !== null}
                />
              ) : (
                <ReviewTaskCard key={task.id} jobId={id!} task={task} />
              ),
            )}
          </div>
        </section>
      )}

      {!hasReviewItems && !isManualRemediation && context.status === "ready" && validationReport?.compliant === true && job.validation_compliant === true && (
        <div className="rounded-xl border border-success/25 bg-success-light/20 p-6">
          <h2 className="text-2xl text-ink tracking-tight mb-2">
            Nothing to review
          </h2>
          <p className="text-sm text-ink-muted">
            This PDF passed all checks. For extra assurance, test with a screen reader or PAC.
          </p>
          <a
            href={apiUrl(`/jobs/${id}/download/report.html`)}
            download={job ? `report_${job.original_filename.replace(/\.pdf$/i, "")}.html` : undefined}
            className="inline-flex mt-4 text-sm text-accent font-medium no-underline hover:underline"
          >
            Download report
          </a>
        </div>
      )}
    </div>
  );
}
