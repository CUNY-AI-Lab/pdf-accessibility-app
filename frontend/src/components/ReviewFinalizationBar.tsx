import { pluralize } from "../utils/format";
import { ArrowRightIcon } from "./Icons";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface AltTextFinalizationBarProps {
  mode: "alt_text";
  allReviewed: boolean;
  pendingCount: number;
  approving: boolean;
  approveError: Error | null;
  onApprove: () => void;
}

export interface ManualReviewFinalizationBarProps {
  mode: "manual_review";
  blockingValidationCount: number;
  pendingBlockingFidelityCount: number;
  finalizable: boolean;
  approving: boolean;
  onApprove: () => void;
}

export type ReviewFinalizationBarProps =
  | AltTextFinalizationBarProps
  | ManualReviewFinalizationBarProps;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ReviewFinalizationBar(props: ReviewFinalizationBarProps) {
  if (props.mode === "alt_text") {
    return (
      <div
        className="
          sticky bottom-6 rounded-xl bg-cream/95 backdrop-blur-sm
          border border-ink/8 shadow-lifted p-5
          flex items-center justify-between
        "
      >
        <div>
          <p className="text-sm font-medium text-ink">
            {props.allReviewed
              ? "All figures reviewed"
              : `${props.pendingCount} ${pluralize(props.pendingCount, "figure")} still need review`}
          </p>
          <p className="text-xs text-ink-muted mt-0.5">
            {props.allReviewed
              ? "Ready to finalize the accessibility checks."
              : "Review all figures to continue."}
          </p>
        </div>
        <button
          type="button"
          onClick={props.onApprove}
          disabled={!props.allReviewed || props.approving}
          className="
            px-6 py-3 rounded-xl
            bg-accent text-white font-semibold text-sm
            hover:bg-accent/90 shadow-sm
            transition-all duration-200
            disabled:opacity-40 disabled:cursor-not-allowed
            flex items-center gap-2
          "
        >
          {props.approving ? (
            <>
              <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round" />
              </svg>
              Finalizing...
            </>
          ) : (
            <>
              Approve &amp; Finalize
              <ArrowRightIcon size={14} />
            </>
          )}
        </button>

        {props.approveError && (
          <p className="absolute -top-10 left-0 text-sm text-error bg-error-light rounded-lg px-4 py-2">
            {props.approveError.message || "Failed to approve"}
          </p>
        )}
      </div>
    );
  }

  // Manual review mode
  return (
    <div
      className="
        sticky bottom-6 rounded-xl bg-cream/95 backdrop-blur-sm
        border border-ink/8 shadow-lifted p-5
        flex items-center justify-between gap-4
      "
    >
      <div>
        <p className="text-sm font-medium text-ink">
          {props.blockingValidationCount > 0
            ? `${props.blockingValidationCount} compliance ${pluralize(props.blockingValidationCount, "issue")} still block release`
            : props.pendingBlockingFidelityCount > 0
              ? `${props.pendingBlockingFidelityCount} required review ${pluralize(props.pendingBlockingFidelityCount, "task")} still need attention`
              : "Manual accessibility review can be finalized"}
        </p>
        <p className="text-xs text-ink-muted mt-0.5">
          {props.blockingValidationCount > 0
            ? "The PDF still has unresolved compliance errors. Those cannot be cleared in this review screen."
            : props.finalizable
              ? "All required accessibility review tasks are resolved."
              : "Resolve the remaining accessibility review tasks to complete the manual review."}
        </p>
      </div>
      <button
        type="button"
        onClick={props.onApprove}
        disabled={!props.finalizable || props.approving}
        className="
          px-6 py-3 rounded-xl
          bg-accent text-white font-semibold text-sm
          hover:bg-accent/90 shadow-sm
          transition-all duration-200
          disabled:opacity-40 disabled:cursor-not-allowed
        "
      >
        {props.approving ? "Finalizing..." : "Finalize Review"}
      </button>
    </div>
  );
}
