import { Link, useNavigate, useParams } from "react-router-dom";
import { useDeleteJob, useJob } from "../api/jobs";
import DownloadButton from "../components/DownloadButton";
import PipelineProgress from "../components/PipelineProgress";
import { useJobProgress } from "../hooks/useJobProgress";

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job, isLoading, error } = useJob(id!);
  const deleteJob = useDeleteJob();
  const isActive = job?.status === "processing" || job?.status === "queued";
  const { steps } = useJobProgress(id!, isActive);

  // Use SSE steps when actively processing, otherwise use API data
  const displaySteps = isActive ? steps : job?.steps ?? [];

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
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Dashboard
      </Link>

      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl md:text-3xl text-ink tracking-tight mb-1">
          {job.original_filename}
        </h1>
        <div className="flex items-center gap-3 text-sm text-ink-muted">
          {job.file_size_bytes && (
            <span>
              {(job.file_size_bytes / (1024 * 1024)).toFixed(1)} MB
            </span>
          )}
          {job.classification && (
            <>
              <span className="opacity-30">&middot;</span>
              <span className="capitalize">{job.classification}</span>
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

      {/* Actions based on status */}
      {job.status === "awaiting_review" && (
        <div className="rounded-xl border border-warning/30 bg-warning-light/30 p-5 mb-6 animate-slide-up">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="font-display text-lg text-ink mb-1">
                Review Required
              </h3>
              <p className="text-sm text-ink-muted">
                Review the generated alt text before finalizing.
              </p>
            </div>
            <Link
              to={`/jobs/${job.id}/review`}
              className="
                px-5 py-2.5 rounded-xl
                bg-accent text-white text-sm font-medium
                hover:bg-accent/90 shadow-sm
                transition-all duration-200 no-underline
              "
            >
              Review &rarr;
            </Link>
          </div>
        </div>
      )}

      {job.status === "complete" && (
        <div className="flex flex-wrap items-center gap-3 animate-slide-up">
          <DownloadButton
            jobId={job.id}
            filename={job.original_filename}
            type="pdf"
          />
          <DownloadButton
            jobId={job.id}
            filename={job.original_filename}
            type="report"
          />
        </div>
      )}

      {job.status === "failed" && (
        <div className="rounded-xl border border-error/20 bg-error-light/30 p-5 animate-slide-up">
          <h3 className="font-display text-lg text-error mb-1">
            Processing Failed
          </h3>
          {job.error && (
            <p className="text-sm text-ink-muted mb-4">{job.error}</p>
          )}
          <button
            type="button"
            onClick={() => {
              if (window.confirm(`Delete this failed job?`)) {
                deleteJob.mutate(job.id, {
                  onSuccess: () => navigate("/dashboard"),
                });
              }
            }}
            className="
              px-4 py-2 rounded-lg text-sm font-medium
              bg-paper-warm text-ink-muted
              hover:bg-error-light hover:text-error transition-colors
            "
          >
            Delete Job
          </button>
        </div>
      )}
    </div>
  );
}
