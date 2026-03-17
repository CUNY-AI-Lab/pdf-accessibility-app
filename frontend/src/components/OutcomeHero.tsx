import { Link } from "react-router-dom";
import type { JobStatus } from "../types";
import { pluralize } from "../utils/format";
import DownloadButton from "./DownloadButton";
import { ArrowRightIcon, CheckIcon, WarningIcon, XIcon } from "./Icons";

type TerminalStatus = Extract<JobStatus, "complete" | "manual_remediation" | "failed">;

interface OutcomeHeroProps {
  jobId: string;
  filename: string;
  status: TerminalStatus;
  appliedChangeCount?: number;
  reviewTaskCount?: number;
  blockingIssueCount?: number | null;
  reviewContextStatus?: "ready" | "loading" | "unavailable";
  error?: string;
}

function inspectSummary(appliedChangeCount: number, reviewTaskCount: number): string | null {
  if (appliedChangeCount > 0 && reviewTaskCount > 0) {
    return `${appliedChangeCount} figure ${pluralize(appliedChangeCount, "decision")} the app already applied and ${reviewTaskCount} visible ${pluralize(reviewTaskCount, "check")}`;
  }
  if (appliedChangeCount > 0) {
    return `${appliedChangeCount} figure ${pluralize(appliedChangeCount, "decision")} the app already applied`;
  }
  if (reviewTaskCount > 0) {
    return `${reviewTaskCount} visible ${pluralize(reviewTaskCount, "check")}`;
  }
  return null;
}

function inspectButtonLabel(appliedChangeCount: number, reviewTaskCount: number): string {
  if (appliedChangeCount > 0 && reviewTaskCount > 0) return "Inspect QA Details";
  if (appliedChangeCount > 0) return "Inspect Figure Decisions";
  return "Inspect Visible Checks";
}

export default function OutcomeHero({
  jobId,
  filename,
  status,
  appliedChangeCount = 0,
  reviewTaskCount = 0,
  blockingIssueCount = null,
  reviewContextStatus = "ready",
  error,
}: OutcomeHeroProps) {
  const summary = inspectSummary(appliedChangeCount, reviewTaskCount);
  const inspectLabel = inspectButtonLabel(appliedChangeCount, reviewTaskCount);

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

  if (status === "complete") {
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
              {reviewContextStatus === "loading"
                ? "This output passed the app's release checks. Optional figure-decision and visible-check details are still loading."
                : reviewContextStatus === "unavailable"
                  ? "This output passed the app's release checks. Figure-decision and visible-check details are unavailable right now."
                  : summary
                ? `This output passed the app's release checks. You can optionally inspect ${summary}.`
                : "This output passed the app's release checks and is ready for assistive technologies."}
            </p>
            <div className="flex flex-wrap items-center gap-3 mt-4">
              <DownloadButton jobId={jobId} filename={filename} type="pdf" />
              {reviewContextStatus === "ready" && summary && (
                <Link
                  to={`/jobs/${jobId}/review`}
                  className="
                    inline-flex items-center gap-2 px-5 py-3 rounded-xl
                    bg-accent text-white font-medium text-sm
                    hover:bg-accent/90 shadow-sm hover:shadow-md
                    transition-all duration-200 no-underline
                  "
                >
                  {inspectLabel}
                  <ArrowRightIcon size={14} />
                </Link>
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

  // Non-compliant output that still needs manual remediation
  return (
    <div className="rounded-2xl border-2 border-warning/25 bg-warning-light/20 p-6 animate-slide-up">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-2xl bg-warning text-white flex items-center justify-center shrink-0">
          <WarningIcon size={22} />
        </div>
        <div className="flex-1">
          <h2 className="font-display text-xl text-ink mb-1">
            Manual remediation required
          </h2>
          <p className="text-sm text-ink-muted leading-relaxed">
            {blockingIssueCount && blockingIssueCount > 0
              ? `${blockingIssueCount} ${pluralize(blockingIssueCount, "issue")} still block a trustworthy accessible output.`
              : "Automated remediation stopped short of a trustworthy accessible output."}
            {" "}
            {reviewContextStatus === "loading"
              ? "Optional figure-decision and visible-check details are still loading."
              : reviewContextStatus === "unavailable"
                ? "Figure-decision and visible-check details are unavailable right now, but manual follow-up is still required outside the app."
              : summary
              ? `You can inspect ${summary} in the current PDF for context, but you will still need manual follow-up outside the app.`
              : "Use the current PDF and report for manual follow-up outside the app."}
          </p>
          <div className="flex flex-wrap items-center gap-3 mt-4">
            <DownloadButton jobId={jobId} filename={filename} type="pdf" />
            {reviewContextStatus === "ready" && summary && (
              <Link
                to={`/jobs/${jobId}/review`}
                className="
                  inline-flex items-center gap-2 px-5 py-3 rounded-xl
                  bg-accent text-white font-medium text-sm
                  hover:bg-accent/90 shadow-sm hover:shadow-md
                  transition-all duration-200 no-underline
                "
              >
                {inspectLabel}
                <ArrowRightIcon size={14} />
              </Link>
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
