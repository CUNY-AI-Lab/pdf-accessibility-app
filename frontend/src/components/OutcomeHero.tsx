import { Link } from "react-router-dom";
import type { JobStatus } from "../types";
import { pluralize } from "../utils/format";
import DownloadButton from "./DownloadButton";
import { ArrowRightIcon, CheckIcon, WarningIcon, XIcon } from "./Icons";

type TerminalStatus = Extract<JobStatus, "complete" | "awaiting_recommendation_review" | "failed">;

interface OutcomeHeroProps {
  jobId: string;
  filename: string;
  status: TerminalStatus;
  compliant?: boolean;
  pendingCount: number;
  error?: string;
}

export default function OutcomeHero({
  jobId,
  filename,
  status,
  compliant,
  pendingCount,
  error,
}: OutcomeHeroProps) {
  if (status === "failed") {
    return (
      <div className="rounded-2xl border-2 border-error/25 bg-error-light/20 p-6 animate-slide-up">
        <div className="flex items-start gap-4">
          <div className="w-12 h-12 rounded-2xl bg-error text-white flex items-center justify-center shrink-0">
            <XIcon size={22} />
          </div>
          <div>
            <h2 className="font-display text-xl text-ink mb-1">
              Processing failed
            </h2>
            {error && (
              <p className="text-sm text-ink-muted leading-relaxed">{error}</p>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (compliant) {
    return (
      <div className="rounded-2xl border-2 border-success/25 bg-success-light/20 p-6 animate-slide-up">
        <div className="flex items-start gap-4">
          <div className="w-12 h-12 rounded-2xl bg-success text-white flex items-center justify-center shrink-0">
            <CheckIcon size={24} />
          </div>
          <div className="flex-1">
            <h2 className="font-display text-xl text-ink mb-1">
              Your PDF is now accessible
            </h2>
            <p className="text-sm text-ink-muted leading-relaxed">
              All PDF/UA compliance checks passed. Your document is ready
              for assistive technologies.
            </p>
            <div className="flex flex-wrap items-center gap-3 mt-4">
              <DownloadButton jobId={jobId} filename={filename} type="pdf" />
              <a
                href={`/api/jobs/${jobId}/download/report`}
                download={`report_${filename}.json`}
                className="text-sm text-ink-muted hover:text-accent transition-colors no-underline"
              >
                Download report
              </a>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Needs review / non-compliant
  return (
    <div className="rounded-2xl border-2 border-warning/25 bg-warning-light/20 p-6 animate-slide-up">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-2xl bg-warning text-white flex items-center justify-center shrink-0">
          <WarningIcon size={22} />
        </div>
        <div className="flex-1">
          <h2 className="font-display text-xl text-ink mb-1">
            {pendingCount > 0
              ? `${pendingCount} ${pluralize(pendingCount, "item")} ${pluralize(pendingCount, "needs", "need")} your attention`
              : "Some issues need your attention"}
          </h2>
          <p className="text-sm text-ink-muted leading-relaxed">
            Automated remediation fixed most issues. A few recommendation-backed
            decisions still need attention before the document is fully accessible.
          </p>
          <div className="flex flex-wrap items-center gap-3 mt-4">
            {status === "awaiting_recommendation_review" ? (
              <Link
                to={`/jobs/${jobId}/review`}
                className="
                  inline-flex items-center gap-2 px-5 py-3 rounded-xl
                  bg-accent text-white font-medium text-sm
                  hover:bg-accent/90 shadow-sm hover:shadow-md
                  transition-all duration-200 no-underline
                "
              >
                Review Recommendations
                <ArrowRightIcon size={14} />
              </Link>
            ) : (
              <DownloadButton jobId={jobId} filename={filename} type="pdf" />
            )}
            <a
              href={`/api/jobs/${jobId}/download/report`}
              download={`report_${filename}.json`}
              className="text-sm text-ink-muted hover:text-accent transition-colors no-underline"
            >
              Download report
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
