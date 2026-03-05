import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAltTexts,
  useApproveReview,
  useJob,
  useUpdateAltText,
} from "../api/jobs";
import AltTextEditor from "../components/AltTextEditor";
import type { AltTextStatus } from "../types";

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const { data: altTexts, isLoading } = useAltTexts(id!, true);
  const updateAltText = useUpdateAltText(id!);
  const approveReview = useApproveReview(id!);
  const [savingFigure, setSavingFigure] = useState<number | null>(null);

  const handleUpdate = (
    figureIndex: number,
    editedText?: string,
    status?: AltTextStatus,
  ) => {
    setSavingFigure(figureIndex);
    updateAltText.mutate(
      { figureIndex, editedText, status },
      { onSettled: () => setSavingFigure(null) },
    );
  };

  const handleApproveAll = async () => {
    try {
      await approveReview.mutateAsync();
      navigate(`/jobs/${id}`);
    } catch {
      // error handled by mutation state
    }
  };

  const allReviewed =
    altTexts?.every((a) => a.status !== "pending_review") ?? false;
  const pendingCount =
    altTexts?.filter((a) => a.status === "pending_review").length ?? 0;

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
    <div className="max-w-3xl mx-auto animate-fade-in">
      {/* Breadcrumb */}
      <Link
        to={`/jobs/${id}`}
        className="
          inline-flex items-center gap-1.5 text-sm text-ink-muted
          hover:text-ink transition-colors no-underline mb-6
        "
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Back to job
      </Link>

      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-2xl md:text-3xl text-ink tracking-tight mb-1">
            Review Alt Text
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
            {altTexts && (
              <span>
                {" "}
                &middot; {altTexts.length} figure
                {altTexts.length !== 1 ? "s" : ""}
              </span>
            )}
          </p>
        </div>

        {/* Progress indicator */}
        {altTexts && altTexts.length > 0 && (
          <div className="text-right">
            <p className="text-sm font-medium text-ink">
              {altTexts.length - pendingCount} / {altTexts.length}
            </p>
            <p className="text-xs text-ink-muted">reviewed</p>
          </div>
        )}
      </div>

      {/* Instructions */}
      <div className="rounded-xl bg-accent-glow border border-accent-light px-5 py-4 mb-8">
        <p className="text-sm text-ink-light leading-relaxed">
          Review each figure's generated alt text. You can approve it as-is,
          edit it for accuracy, or mark purely decorative images. All figures
          must be reviewed before finalizing.
        </p>
      </div>

      {/* Alt text editors */}
      {altTexts && altTexts.length > 0 ? (
        <div className="space-y-4 mb-8 stagger">
          {altTexts.map((altText) => (
            <AltTextEditor
              key={`${altText.id}-${altText.edited_text ?? ""}-${altText.generated_text ?? ""}-${altText.status}`}
              altText={altText}
              onUpdate={handleUpdate}
              saving={savingFigure === altText.figure_index}
            />
          ))}
        </div>
      ) : (
        <div className="text-center py-16 rounded-xl bg-cream border border-ink/6">
          <div className="w-12 h-12 rounded-2xl bg-success-light text-success mx-auto mb-4 flex items-center justify-center">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          </div>
          <h3 className="font-display text-lg text-ink mb-1">
            No figures found
          </h3>
          <p className="text-sm text-ink-muted">
            This document has no images requiring alt text.
          </p>
        </div>
      )}

      {/* Approve & Finalize */}
      {altTexts && altTexts.length > 0 && (
        <div
          className="
            sticky bottom-6 rounded-xl bg-cream/95 backdrop-blur-sm
            border border-ink/8 shadow-lifted p-5
            flex items-center justify-between
          "
        >
          <div>
            <p className="text-sm font-medium text-ink">
              {allReviewed
                ? "All figures reviewed"
                : `${pendingCount} figure${pendingCount !== 1 ? "s" : ""} still need review`}
            </p>
            <p className="text-xs text-ink-muted mt-0.5">
              {allReviewed
                ? "Ready to finalize tagging and validation."
                : "Review all figures to continue."}
            </p>
          </div>
          <button
            type="button"
            onClick={handleApproveAll}
            disabled={!allReviewed || approveReview.isPending}
            className="
              px-6 py-3 rounded-xl
              bg-accent text-white font-semibold text-sm
              hover:bg-accent/90 shadow-sm
              transition-all duration-200
              disabled:opacity-40 disabled:cursor-not-allowed
              flex items-center gap-2
            "
          >
            {approveReview.isPending ? (
              <>
                <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round" />
                </svg>
                Finalizing...
              </>
            ) : (
              <>
                Approve &amp; Finalize
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="5" y1="12" x2="19" y2="12" />
                  <polyline points="12 5 19 12 12 19" />
                </svg>
              </>
            )}
          </button>

          {approveReview.isError && (
            <p className="absolute -top-10 left-0 text-sm text-error bg-error-light rounded-lg px-4 py-2">
              {approveReview.error?.message || "Failed to approve"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
