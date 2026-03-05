import type { ValidationReport as ValidationReportType } from "../types";

interface ValidationReportProps {
  report: ValidationReportType;
}

export default function ValidationReport({ report }: ValidationReportProps) {
  return (
    <div className="space-y-4">
      {/* Summary card */}
      <div
        className={`
          rounded-xl border p-5
          ${
            report.compliant
              ? "border-success/30 bg-success-light/30"
              : "border-warning/30 bg-warning-light/30"
          }
        `}
      >
        <div className="flex items-center gap-3">
          {report.compliant ? (
            <div className="w-10 h-10 rounded-xl bg-success text-white flex items-center justify-center">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
          ) : (
            <div className="w-10 h-10 rounded-xl bg-warning text-white flex items-center justify-center">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            </div>
          )}
          <div>
            <h3 className="font-display text-lg font-semibold">
              {report.compliant
                ? "PDF/UA Compliant"
                : "Issues Found"}
            </h3>
            <p className="text-sm text-ink-muted">
              {report.violations.length === 0
                ? "All validation checks passed."
                : `${report.violations.length} issue${report.violations.length === 1 ? "" : "s"} require attention.`}
            </p>
          </div>
        </div>
      </div>

      {/* Violations list */}
      {report.violations.length > 0 && (
        <div className="rounded-xl border border-ink/6 bg-cream overflow-hidden">
          <div className="px-4 py-3 border-b border-ink/6 bg-paper-warm/50">
            <h4 className="text-sm font-semibold text-ink">
              Validation Issues
            </h4>
          </div>
          <div className="divide-y divide-ink/5">
            {report.violations.map((v, i) => (
              <div key={i} className="px-4 py-3 flex items-start gap-3">
                <span
                  className={`
                    mt-0.5 w-5 h-5 rounded flex items-center justify-center text-[10px] font-bold shrink-0
                    ${v.severity === "error" ? "bg-error-light text-error" : "bg-warning-light text-warning"}
                  `}
                >
                  {v.severity === "error" ? "!" : "?"}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-ink">{v.description}</p>
                  <div className="flex items-center gap-3 mt-1">
                    <span className="text-xs font-mono text-ink-muted">
                      {v.rule_id}
                    </span>
                    {v.count > 1 && (
                      <span className="text-xs text-ink-muted">
                        {v.count} occurrences
                      </span>
                    )}
                    {v.location && (
                      <span className="text-xs text-ink-muted truncate">
                        {v.location}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
