import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useAppliedChanges, useDeleteJob, useJob, useReviewTasks, useValidation } from "../api/jobs";
import ConfirmDialog from "../components/ConfirmDialog";
import { ChevronLeftIcon, ChevronRightIcon } from "../components/Icons";
import OutcomeHero from "../components/OutcomeHero";
import PipelineProgress from "../components/PipelineProgress";
import RemediationSummary from "../components/RemediationSummary";
import ValidationReport from "../components/ValidationReport";
import { useJobProgress } from "../hooks/useJobProgress";
import { formatBytes, formatClassification } from "../utils/format";
import { asNumber } from "../utils/typeGuards";

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job, isLoading, error } = useJob(id!);
  const deleteJob = useDeleteJob();
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const isActive = job?.status === "processing" || job?.status === "queued";
  const canInspectOutput = job?.status === "complete" || job?.status === "manual_remediation";
  const { steps } = useJobProgress(id!, isActive, job?.steps);
  const hasFinalOutput = job?.status === "complete" || job?.status === "manual_remediation";
  const {
    data: validationReport,
    error: validationError,
  } = useValidation(id!, hasFinalOutput);
  const {
    data: reviewTasks,
    error: reviewTasksError,
    isLoading: reviewTasksLoading,
  } = useReviewTasks(id!, canInspectOutput);
  const {
    data: appliedChanges,
    error: appliedChangesError,
    isLoading: appliedChangesLoading,
  } = useAppliedChanges(id!, canInspectOutput);
  const openReviewTasks = reviewTasks?.filter((task) => task.status === "pending_review") ?? [];
  const pendingAppliedChanges = appliedChanges?.filter((change) => change.review_status === "pending_review") ?? [];
  const followUpCount = openReviewTasks.length;
  const appliedChangeCount = pendingAppliedChanges.length;
  const [showDetails, setShowDetails] = useState(false);
  const reviewContextLoading =
    canInspectOutput && (reviewTasksLoading || appliedChangesLoading);
  const reviewContextError = reviewTasksError || appliedChangesError;
  const reviewContextStatus =
    !canInspectOutput
      ? "ready"
      : reviewContextLoading
        ? "loading"
        : reviewContextError
          ? "unavailable"
          : "ready";
  const fidelity =
    validationReport?.fidelity && typeof validationReport.fidelity === "object"
      ? (validationReport.fidelity as Record<string, unknown>)
      : {};
  const fidelitySummary =
    fidelity.summary && typeof fidelity.summary === "object"
      ? (fidelity.summary as Record<string, unknown>)
      : {};
  const blockingIssueCount =
    asNumber(fidelitySummary.blocking_tasks) ?? null;

  // Use SSE steps when actively processing, otherwise use API data
  const displaySteps = isActive ? steps : job?.steps ?? [];
  const taggingResult = displaySteps.find((s) => s.step_name === "tagging")?.result;

  if (isLoading) {
    return (
      <div className="max-w-2xl mx-auto animate-pulse-soft">
        <div className="h-6 bg-paper-warm rounded w-48 mb-2" />
        <div className="h-4 bg-paper-warm rounded w-32 mb-8" />
        <div className="space-y-4">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="flex gap-4">
              <div className="w-10 h-10 rounded-xl bg-paper-warm" />
              <div className="flex-1 pt-2">
                <div className="h-4 bg-paper-warm rounded w-24 mb-1" />
                <div className="h-3 bg-paper-warm rounded w-48" />
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (error || !job) {
    return (
      <div className="text-center py-20">
        <h2 className="text-xl font-display text-ink mb-2">Job not found</h2>
        <p className="text-sm text-ink-muted mb-6">
          This job may have been deleted or doesn't exist.
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

  return (
    <div className="max-w-2xl mx-auto animate-fade-in">
      {/* Breadcrumb */}
      <Link
        to="/dashboard"
        className="
          inline-flex items-center gap-1.5 text-sm text-ink-muted
          hover:text-ink transition-colors no-underline mb-6
        "
      >
        <ChevronLeftIcon size={14} />
        Dashboard
      </Link>

      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl md:text-3xl text-ink tracking-tight mb-1">
          {job.original_filename}
        </h1>
        <div className="flex items-center gap-3 text-sm text-ink-muted">
          {job.file_size_bytes && (
            <span>{formatBytes(job.file_size_bytes)}</span>
          )}
          {job.classification && (
            <>
              <span className="opacity-30">&middot;</span>
              <span>{formatClassification(job.classification)}</span>
            </>
          )}
          {job.page_count && (
            <>
              <span className="opacity-30">&middot;</span>
              <span>{job.page_count} pages</span>
            </>
          )}
        </div>
      </div>

      {/* Pipeline progress */}
      <div className="rounded-xl border border-ink/6 bg-cream p-6 mb-6">
        <h2 className="text-lg font-display text-ink mb-5">
          Pipeline Progress
        </h2>
        <PipelineProgress steps={displaySteps} />
      </div>

      {hasFinalOutput && (
        <div className="space-y-6 animate-slide-up">
          {/* Layer 1: Outcome Hero */}
          <OutcomeHero
            jobId={job.id}
            filename={job.original_filename}
            status={job.status as "complete" | "manual_remediation" | "failed"}
            appliedChangeCount={appliedChangeCount}
            reviewTaskCount={followUpCount}
            blockingIssueCount={job.status === "manual_remediation" ? blockingIssueCount : null}
            reviewContextStatus={reviewContextStatus}
          />

          {validationError && (
            <div className="rounded-xl border border-warning/30 bg-warning-light/20 p-5">
              <h3 className="font-display text-lg text-ink mb-1">
                Detailed report unavailable
              </h3>
              <p className="text-sm text-ink-muted">
                Could not load the full validation details right now. The job status
                above still reflects the final outcome.
              </p>
            </div>
          )}

          {reviewContextStatus === "unavailable" && (
            <div className="rounded-xl border border-warning/30 bg-warning-light/20 p-5">
              <h3 className="font-display text-lg text-ink mb-1">
                Review details unavailable
              </h3>
              <p className="text-sm text-ink-muted">
                Could not load review details right now. Reload the page to try again.
              </p>
            </div>
          )}

          {/* Layer 2: What We Did */}
          {validationReport && (
            <RemediationSummary
              report={validationReport}
              classification={job.classification}
            />
          )}

          {/* Layer 3: Collapsible Technical Details */}
          {validationReport && (
            <div>
              <button
                type="button"
                onClick={() => setShowDetails((prev) => !prev)}
                className="
                  flex items-center gap-2 text-sm text-ink-muted
                  hover:text-ink transition-colors py-2
                "
              >
                <ChevronRightIcon
                  size={12}
                  className={`transition-transform duration-200 ${showDetails ? "rotate-90" : ""}`}
                />
                Technical Details
              </button>

              {showDetails && (
                <div className="space-y-6 mt-3 animate-fade-in">
                  <div className="rounded-xl border border-ink/6 bg-cream p-5">
                    <h3 className="font-display text-lg text-ink mb-4">
                      Accessibility Metadata
                    </h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Standard</p>
                        <p className="text-ink mt-0.5">
                          {validationReport.standard || "PDF/UA"}
                          {validationReport.profile ? ` (${validationReport.profile})` : ""}
                        </p>
                      </div>
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Validator</p>
                        <p className="text-ink mt-0.5">
                          {validationReport.validator || "unknown"}
                        </p>
                      </div>
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Links Tagged</p>
                        <p className="text-ink mt-0.5">
                          {asNumber(taggingResult?.links_tagged) ?? "n/a"}
                        </p>
                      </div>
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Decorative Figures Artifacted</p>
                        <p className="text-ink mt-0.5">
                          {asNumber(taggingResult?.decorative_figures_artifacted) ?? "n/a"}
                        </p>
                      </div>
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Bookmarks Added</p>
                        <p className="text-ink mt-0.5">
                          {asNumber(taggingResult?.bookmarks_added) ?? "n/a"}
                        </p>
                      </div>
                      <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
                        <p className="text-ink-muted text-xs">Report Timestamp</p>
                        <p className="text-ink mt-0.5">
                          {validationReport.generated_at
                            ? new Date(validationReport.generated_at).toLocaleString()
                            : "n/a"}
                        </p>
                      </div>
                    </div>
                    <p className="text-xs text-ink-muted mt-4">
                      Automated validation is a strong signal but not a complete substitute
                      for manual assistive-technology testing.
                    </p>
                  </div>

                  <ValidationReport report={validationReport} />
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {job.status === "failed" && (
        <div className="space-y-4">
          <OutcomeHero
            jobId={job.id}
            filename={job.original_filename}
            status="failed"
            appliedChangeCount={0}
            reviewTaskCount={0}
            error={job.error}
          />
          <button
            type="button"
            onClick={() => {
              setDeleteError(null);
              setShowConfirmDialog(true);
            }}
            className="
              px-4 py-2 rounded-lg text-sm font-medium
              bg-paper-warm text-ink-muted
              hover:bg-error-light hover:text-error transition-colors
            "
          >
            Delete Job
          </button>
          <ConfirmDialog
            open={showConfirmDialog}
            title="Delete Job"
            message="Delete this failed job? This action cannot be undone."
            confirmLabel="Delete"
            cancelLabel="Cancel"
            confirmPending={deleteJob.isPending}
            errorMessage={deleteError}
            onConfirm={() => {
              setDeleteError(null);
              deleteJob.mutate(job.id, {
                onSuccess: () => {
                  setShowConfirmDialog(false);
                  navigate("/dashboard");
                },
                onError: (error) => {
                  setDeleteError(
                    error instanceof Error ? error.message : "Failed to delete this job. Please try again.",
                  );
                },
              });
            }}
            onCancel={() => {
              if (deleteJob.isPending) return;
              setShowConfirmDialog(false);
            }}
          />
        </div>
      )}
    </div>
  );
}
