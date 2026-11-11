import { Link } from "react-router-dom";
import { apiUrl } from "../api/client";
import { complianceOutcome, type ReviewContext } from "../state/jobView";
import type { TerminalJobStatus } from "../types";
import { pluralize } from "../utils/format";
import DownloadButton from "./DownloadButton";
import { ArrowRightIcon, CheckIcon, WarningIcon, XIcon } from "./Icons";

interface OutcomeHeroProps {
  jobId: string;
  filename: string;
  status: TerminalJobStatus;
  hasHtml?: boolean;
  validationCompliant?: boolean;
  appliedChangeCount?: number;
  reviewTaskCount?: number;
  blockingIssueCount?: number | null;
  reviewContextStatus?: ReviewContext<unknown>["status"];
  error?: string;
}

function reviewSummary(appliedChangeCount: number, reviewTaskCount: number): string | null {
  if (appliedChangeCount > 0 && reviewTaskCount > 0) {
    return `${appliedChangeCount} image ${pluralize(appliedChangeCount, "description")} and ${reviewTaskCount} additional ${pluralize(reviewTaskCount, "check")}`;
  }
  if (appliedChangeCount > 0) {
    return `${appliedChangeCount} image ${pluralize(appliedChangeCount, "description")}`;
  }
  if (reviewTaskCount > 0) {
    return `${reviewTaskCount} additional ${pluralize(reviewTaskCount, "check")}`;
  }
  return null;
}

function reviewButtonLabel(appliedChangeCount: number): string {
  if (appliedChangeCount > 0) return "Review Image Descriptions";
  return "Review Checks";
}

export default function OutcomeHero({
  jobId,
  filename,
  status,
  hasHtml = false,
  validationCompliant = false,
  appliedChangeCount = 0,
  reviewTaskCount = 0,
  blockingIssueCount = null,
  reviewContextStatus = "not_applicable",
  error,
}: OutcomeHeroProps) {
  const outcome = complianceOutcome(status, validationCompliant, reviewContextStatus);
  const summary = reviewSummary(appliedChangeCount, reviewTaskCount);
  const reviewLabel = reviewButtonLabel(appliedChangeCount);

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
              {outcome === "passed" ? "Your PDF is ready" : "Processing finished"}
            </h2>
            <p className="text-sm text-ink-muted leading-relaxed">
              {reviewContextStatus !== "ready"
                ? "Review details and validation are not available yet."
                : outcome !== "passed"
                  ? "The output is available. Check the report for unresolved or unverified compliance issues."
                  : summary
                    ? `Automated compliance checks passed. You can review ${summary}.`
                    : "Automated compliance checks passed. Review the PDF with assistive technologies before sharing."}
            </p>
            <div className="flex flex-wrap items-center gap-3 mt-4">
              <DownloadButton jobId={jobId} filename={filename} type="pdf" />
              {hasHtml && <DownloadButton jobId={jobId} filename={filename} type="html" />}
              {(reviewContextStatus !== "ready" || summary) && (
                <Link
                  to={`/jobs/${jobId}/review`}
                  className="
                    inline-flex items-center gap-2 px-5 py-3 rounded-xl
                    border border-ink/10 bg-white text-ink font-medium text-sm
                    hover:border-ink/20 hover:bg-paper-warm
                    shadow-sm hover:shadow-card
                    transition-all duration-200 no-underline
                  "
                >
                  {reviewLabel}
                  <ArrowRightIcon size={14} />
                </Link>
              )}
              <a
                href={apiUrl(`/jobs/${jobId}/download/report.html`)}
                download={`report_${filename.replace(/\.pdf$/i, "")}.html`}
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

  // Needs manual fixes
  return (
    <div className="rounded-2xl border-2 border-warning/25 bg-warning-light/20 p-6 animate-slide-up">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 rounded-2xl bg-warning text-white flex items-center justify-center shrink-0">
          <WarningIcon size={22} />
        </div>
        <div className="flex-1">
          <h2 className="font-display text-xl text-ink mb-1">
            Needs manual fixes
          </h2>
          <p className="text-sm text-ink-muted leading-relaxed">
            {blockingIssueCount && blockingIssueCount > 0
              ? `${blockingIssueCount} ${pluralize(blockingIssueCount, "issue")} could not be fixed automatically.`
              : "Some issues could not be fixed automatically."}
            {" "}Download the current PDF and report to continue remediation in Acrobat or another tool.
          </p>
          <div className="flex flex-wrap items-center gap-3 mt-4">
            <DownloadButton jobId={jobId} filename={filename} type="pdf" />
            {hasHtml && <DownloadButton jobId={jobId} filename={filename} type="html" />}
            {(reviewContextStatus !== "ready" || summary) && (
              <Link
                to={`/jobs/${jobId}/review`}
                className="
                  inline-flex items-center gap-2 px-5 py-3 rounded-xl
                  border border-ink/10 bg-white text-ink font-medium text-sm
                  hover:border-ink/20 hover:bg-paper-warm
                  shadow-sm hover:shadow-card
                  transition-all duration-200 no-underline
                "
              >
                {reviewLabel}
                <ArrowRightIcon size={14} />
              </Link>
            )}
            <a
              href={apiUrl(`/jobs/${jobId}/download/report.html`)}
              download={`report_${filename.replace(/\.pdf$/i, "")}.html`}
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
