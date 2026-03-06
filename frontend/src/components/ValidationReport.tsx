import type { ValidationChange, ValidationReport as ValidationReportType } from "../types";

interface ValidationReportProps {
  report: ValidationReportType;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function statusLabel(status: ValidationChange["remediation_status"]): string {
  if (status === "auto_remediated") return "Auto Remediated";
  if (status === "manual_remediated") return "Manual Remediated";
  return "Needs Remediation";
}

function statusTone(status: ValidationChange["remediation_status"]): string {
  if (status === "auto_remediated") return "bg-success-light text-success";
  if (status === "manual_remediated") return "bg-info-light text-info";
  return "bg-warning-light text-warning";
}

export default function ValidationReport({ report }: ValidationReportProps) {
  const baseline =
    report.baseline && typeof report.baseline === "object"
      ? (report.baseline as Record<string, unknown>)
      : {};
  const baselineSummary =
    baseline.summary && typeof baseline.summary === "object"
      ? (baseline.summary as Record<string, unknown>)
      : {};
  const remediation =
    report.remediation && typeof report.remediation === "object"
      ? (report.remediation as Record<string, unknown>)
      : {};
  const fidelity =
    report.fidelity && typeof report.fidelity === "object"
      ? (report.fidelity as Record<string, unknown>)
      : {};
  const fontRemediation =
    remediation.font_remediation && typeof remediation.font_remediation === "object"
      ? (remediation.font_remediation as Record<string, unknown>)
      : {};
  const fidelitySummary =
    fidelity.summary && typeof fidelity.summary === "object"
      ? (fidelity.summary as Record<string, unknown>)
      : {};
  const fidelityChecks = Array.isArray(fidelity.checks)
    ? (fidelity.checks as Array<Record<string, unknown>>)
    : [];
  const fidelityPassed = asBool(fidelity.passed);
  const blockingTasks = asNumber(fidelitySummary.blocking_tasks);
  const advisoryTasks = asNumber(fidelitySummary.advisory_tasks);

  const baselineErrors = asNumber(baselineSummary.errors);
  const baselineWarnings = asNumber(baselineSummary.warnings);
  const postErrors = asNumber(remediation.post_errors) ?? asNumber(report.summary.errors);
  const postWarnings = asNumber(remediation.post_warnings) ?? asNumber(report.summary.warnings);
  const autoRemediated = asNumber(remediation.auto_remediated);
  const needsRemediation = asNumber(remediation.needs_remediation);
  const errorsReduced = asNumber(remediation.errors_reduced);
  const fontAttempted = asBool(fontRemediation.attempted);
  const fontApplied = asBool(fontRemediation.applied);
  const selectedLane = asString(fontRemediation.selected_lane);
  const lanesPlanned = Array.isArray(fontRemediation.lanes_planned)
    ? fontRemediation.lanes_planned.length
    : 0;
  const laneResults = Array.isArray(fontRemediation.lane_results)
    ? fontRemediation.lane_results.length
    : 0;
  const changes = [...(report.changes ?? [])].sort((a, b) => {
    const order = {
      needs_remediation: 0,
      manual_remediated: 1,
      auto_remediated: 2,
    };
    return order[a.remediation_status] - order[b.remediation_status];
  });

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

      {(baselineErrors !== null || postErrors !== null) && (
        <div className="rounded-xl border border-ink/6 bg-cream p-4">
          <h4 className="text-sm font-semibold text-ink mb-3">
            Before/After Validation
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Baseline</p>
              <p className="text-ink mt-0.5">
                {baselineErrors ?? "n/a"} errors
                {baselineWarnings !== null ? `, ${baselineWarnings} warnings` : ""}
              </p>
            </div>
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Remediated Output</p>
              <p className="text-ink mt-0.5">
                {postErrors ?? "n/a"} errors
                {postWarnings !== null ? `, ${postWarnings} warnings` : ""}
              </p>
            </div>
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Delta</p>
              <p className="text-ink mt-0.5">
                {errorsReduced !== null ? `${errorsReduced >= 0 ? "+" : ""}${errorsReduced} errors reduced` : "n/a"}
              </p>
              {(autoRemediated !== null || needsRemediation !== null) && (
                <p className="text-xs text-ink-muted mt-0.5">
                  auto: {autoRemediated ?? "n/a"} | remaining: {needsRemediation ?? "n/a"}
                </p>
              )}
              {fontAttempted && (
                <p className="text-xs text-ink-muted mt-0.5">
                  font lanes:{" "}
                  {fontApplied
                    ? `applied${selectedLane ? ` (${selectedLane})` : ""}`
                    : "attempted (not applied)"}
                  {lanesPlanned > 0 ? ` | planned: ${lanesPlanned}` : ""}
                  {laneResults > 0 ? ` | attempts: ${laneResults}` : ""}
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {(fidelityPassed !== null || fidelityChecks.length > 0) && (
        <div className="rounded-xl border border-ink/6 bg-cream p-4">
          <h4 className="text-sm font-semibold text-ink mb-3">
            Fidelity Gate
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Status</p>
              <p className="text-ink mt-0.5">
                {fidelityPassed === null
                  ? "n/a"
                  : fidelityPassed
                    ? "Passed"
                    : "Manual review required"}
              </p>
            </div>
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Blocking Tasks</p>
              <p className="text-ink mt-0.5">{blockingTasks ?? "n/a"}</p>
            </div>
            <div className="rounded-lg bg-paper-warm/60 px-3 py-2">
              <p className="text-ink-muted text-xs">Advisory Tasks</p>
              <p className="text-ink mt-0.5">{advisoryTasks ?? "n/a"}</p>
            </div>
          </div>
          {fidelityChecks.length > 0 && (
            <div className="mt-4 space-y-2">
              {fidelityChecks.map((check, index) => {
                const status = asString(check.status) ?? "skip";
                const message = asString(check.message) ?? "No detail available.";
                const label = asString(check.check) ?? `check-${index}`;
                const metrics =
                  check.metrics && typeof check.metrics === "object"
                    ? (check.metrics as Record<string, unknown>)
                    : {};
                return (
                  <div
                    key={`${label}-${index}`}
                    className="rounded-lg bg-paper-warm/60 px-3 py-2"
                  >
                    <div className="flex items-center gap-2 justify-between">
                      <p className="text-sm text-ink capitalize">
                        {label.replaceAll("_", " ")}
                      </p>
                      <span
                        className={`
                          text-[11px] px-2 py-1 rounded-full
                          ${
                            status === "pass"
                              ? "bg-success-light text-success"
                              : status === "fail"
                                ? "bg-error-light text-error"
                                : status === "warning"
                                  ? "bg-warning-light text-warning"
                                  : "bg-paper-warm text-ink-muted"
                          }
                        `}
                      >
                        {status}
                      </span>
                    </div>
                    <p className="text-xs text-ink-muted mt-1">{message}</p>
                    {Object.keys(metrics).length > 0 && (
                      <p className="text-xs text-ink-muted mt-1 font-mono">
                        {Object.entries(metrics)
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(" | ")}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {changes.length > 0 && (
        <div className="rounded-xl border border-ink/6 bg-cream overflow-hidden">
          <div className="px-4 py-3 border-b border-ink/6 bg-paper-warm/50">
            <h4 className="text-sm font-semibold text-ink">
              Remediation Lifecycle
            </h4>
          </div>
          <div className="divide-y divide-ink/5">
            {changes.map((c, i) => (
              <div key={`${c.rule_id}-${i}`} className="px-4 py-3">
                <div className="flex items-start gap-2 justify-between">
                  <div className="min-w-0">
                    <p className="text-sm text-ink">{c.description}</p>
                    {c.fix_hint && (
                      <p className="text-xs text-ink-muted mt-1">
                        Suggested fix: {c.fix_hint}
                      </p>
                    )}
                  </div>
                  <span className={`shrink-0 text-[11px] px-2 py-1 rounded-full ${statusTone(c.remediation_status)}`}>
                    {statusLabel(c.remediation_status)}
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1">
                  <span className="text-xs font-mono text-ink-muted">
                    {c.rule_id}
                  </span>
                  {c.category && (
                    <span className="text-xs text-ink-muted capitalize">
                      {c.category}
                    </span>
                  )}
                  <span className="text-xs text-ink-muted">
                    {c.baseline_count} &rarr; {c.post_count}
                  </span>
                  {c.location && (
                    <span className="text-xs text-ink-muted truncate">
                      {c.location}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

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
                  {v.fix_hint && (
                    <p className="text-xs text-ink-muted mt-1">
                      Suggested fix: {v.fix_hint}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-1">
                    <span className="text-xs font-mono text-ink-muted">
                      {v.rule_id}
                    </span>
                    {v.category && (
                      <span className="text-xs text-ink-muted capitalize">
                        {v.category}
                      </span>
                    )}
                    {v.remediation_status && (
                      <span className={`text-xs px-1.5 py-0.5 rounded ${statusTone(v.remediation_status)}`}>
                        {statusLabel(v.remediation_status)}
                      </span>
                    )}
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
