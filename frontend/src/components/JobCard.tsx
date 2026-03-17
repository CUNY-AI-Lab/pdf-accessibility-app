import { useState } from "react";
import { Link } from "react-router-dom";
import { useDeleteJob } from "../api/jobs";
import { formatBytes, formatDate } from "../utils/format";
import ConfirmDialog from "./ConfirmDialog";
import type { Job, JobStatus } from "../types";
import { CheckIcon } from "./Icons";

const STATUS_CONFIG: Record<
  JobStatus,
  { label: string; color: string; bg: string; dot: string }
> = {
  queued: {
    label: "Queued",
    color: "text-ink-muted",
    bg: "bg-paper-warm",
    dot: "bg-ink-muted",
  },
  processing: {
    label: "Processing",
    color: "text-info",
    bg: "bg-info-light",
    dot: "bg-info animate-pulse-soft",
  },
  manual_remediation: {
    label: "Needs Fixes",
    color: "text-warning",
    bg: "bg-warning-light",
    dot: "bg-warning",
  },
  complete: {
    label: "Complete",
    color: "text-success",
    bg: "bg-success-light",
    dot: "bg-success",
  },
  failed: {
    label: "Failed",
    color: "text-error",
    bg: "bg-error-light",
    dot: "bg-error",
  },
};

interface JobCardProps {
  job: Job;
}

export default function JobCard({ job }: JobCardProps) {
  const config = STATUS_CONFIG[job.status];
  const deleteJob = useDeleteJob();
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const completedSteps = job.steps.filter(
    (s) => s.status === "complete" || s.status === "skipped",
  ).length;
  const progress = job.steps.length > 0 ? (completedSteps / job.steps.length) * 100 : 0;

  const isCompliant = job.validation_compliant ?? null;
  const linkTo = `/jobs/${job.id}`;

  const handleDelete = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDeleteError(null);
    setShowConfirmDialog(true);
  };

  return (
    <>
      <Link
        to={linkTo}
        className="
          group block rounded-xl bg-cream border border-ink/6
          p-5 no-underline
          transition-all duration-200 ease-out
          hover:shadow-card hover:border-ink/10 hover:-translate-y-0.5
          active:translate-y-0
        "
      >
        {/* Top row: filename + status */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0 flex-1">
            <h3 className="font-medium text-ink text-sm truncate">
              {job.original_filename}
            </h3>
            <div className="flex items-center gap-2 mt-1 text-xs text-ink-muted">
              {job.file_size_bytes && (
                <span>{formatBytes(job.file_size_bytes)}</span>
              )}
              {job.page_count && (
                <>
                  <span className="opacity-30">&middot;</span>
                  <span>{job.page_count} pages</span>
                </>
              )}
              {job.classification && (
                <>
                  <span className="opacity-30">&middot;</span>
                  <span className="capitalize">{job.classification}</span>
                </>
              )}
            </div>
          </div>

          <span
            className={`
              inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium
              ${config.color} ${config.bg}
            `}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${config.dot}`} />
            {config.label}
          </span>
        </div>

        {/* Progress bar */}
        {(job.status === "processing" || job.status === "queued") && (
          <div className="mb-3">
            <div className="h-1.5 bg-paper-warm rounded-full overflow-hidden">
              <div
                className="h-full bg-accent-bright rounded-full transition-all duration-500 ease-out"
                style={{ width: `${Math.max(progress, 8)}%` }}
              />
            </div>
            <p className="text-xs text-ink-muted mt-1">
              {completedSteps} of {job.steps.length} steps complete
            </p>
          </div>
        )}

        {/* Outcome summary for finished jobs */}
        {job.status === "complete" && isCompliant === true && (
          <p className="text-xs text-success font-medium mb-3 flex items-center gap-1.5">
            <CheckIcon size={12} />
            Fully compliant
          </p>
        )}
        {job.status === "complete" && isCompliant === false && (
          <p className="text-xs text-warning font-medium mb-3">
            Some issues remain
          </p>
        )}

        {job.status === "manual_remediation" && (
          <p className="text-xs text-warning font-medium mb-3">
            Needs manual fixes
          </p>
        )}

        {/* Error message */}
        {job.error && (
          <p className="text-xs text-error bg-error-light rounded-lg px-3 py-2 mb-3 line-clamp-2">
            {job.error}
          </p>
        )}

        {deleteError && (
          <p className="text-xs text-error bg-error-light rounded-lg px-3 py-2 mb-3">
            {deleteError}
          </p>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between">
          <span className="text-xs text-ink-muted">
            {formatDate(job.updated_at)}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleDelete}
              className="
                text-xs text-ink-muted/0 group-hover:text-ink-muted
                hover:!text-error transition-all p-1 rounded
              "
              aria-label={`Delete ${job.original_filename}`}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              </svg>
            </button>
            <span className="text-xs text-accent font-medium opacity-0 group-hover:opacity-100 transition-opacity">
              View &rarr;
            </span>
          </div>
        </div>
      </Link>
      <ConfirmDialog
        open={showConfirmDialog}
        title="Delete Job"
        message={`Delete "${job.original_filename}"? This action cannot be undone.`}
        confirmLabel="Delete"
        cancelLabel="Cancel"
        confirmPending={deleteJob.isPending}
        errorMessage={deleteError}
        onConfirm={() => {
          setDeleteError(null);
          deleteJob.mutate(job.id, {
            onSuccess: () => {
              setShowConfirmDialog(false);
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
    </>
  );
}
