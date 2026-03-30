import type { ReviewTask } from "../types";
import { pagePreviewUrl, previewPagesForTask } from "../pages/reviewHelpers";
import PreviewImage from "./PreviewImage";

interface FidelityIssueCardProps {
  jobId: string;
  task: ReviewTask;
  onResolve?: (taskId: number) => void;
  resolving?: boolean;
}

function num(task: ReviewTask, key: string): number {
  const value = task.metadata?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function pages(task: ReviewTask): number[] {
  const value = task.metadata?.pages_to_check;
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === "number" ? item : Number(item)))
    .filter((item) => Number.isFinite(item) && item > 0);
}

function strList(task: ReviewTask, key: string): string[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink">{value}</dd>
    </div>
  );
}

function ContentFidelityDetails({ task }: { task: ReviewTask }) {
  const similarity = num(task, "similarity");
  const lengthRatio = num(task, "length_ratio");
  const outputChars = num(task, "output_chars");
  const containment = num(task, "containment");
  const preservation = num(task, "preservation");
  const hasStats = similarity > 0 || lengthRatio > 0 || outputChars > 0 || containment > 0 || preservation > 0;
  if (!hasStats) return null;
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {containment > 0 && <Stat label="Text containment" value={`${Math.round(containment)}%`} />}
      {preservation > 0 && <Stat label="Text preservation" value={pct(preservation)} />}
      {similarity > 0 && <Stat label="Text similarity" value={pct(similarity)} />}
      {lengthRatio > 0 && <Stat label="Length ratio" value={lengthRatio.toFixed(2)} />}
      {outputChars > 0 && <Stat label="Output characters" value={outputChars.toLocaleString()} />}
    </dl>
  );
}

function ReadingOrderDetails({ task }: { task: ReviewTask }) {
  const hitRate = num(task, "hit_rate");
  const orderRate = num(task, "order_rate");
  const fragments = num(task, "fragments_considered");
  if (!hitRate && !orderRate) return null;
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {hitRate > 0 && <Stat label="Fragment hit rate" value={pct(hitRate)} />}
      {orderRate > 0 && <Stat label="Order preservation" value={pct(orderRate)} />}
      {fragments > 0 && <Stat label="Fragments tested" value={fragments} />}
    </dl>
  );
}

function TableSemanticsDetails({ task }: { task: ReviewTask }) {
  const detected = num(task, "detected_tables");
  const tagged = num(task, "tagged_tables");
  const coverage = num(task, "coverage");
  const complexTables = num(task, "complex_tables");
  const riskScore = num(task, "risk_score");
  if (!detected && !complexTables) return null;
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {detected > 0 && <Stat label="Tables detected" value={detected} />}
      {tagged > 0 && <Stat label="Tables tagged" value={tagged} />}
      {coverage > 0 && <Stat label="Coverage" value={pct(coverage)} />}
      {complexTables > 0 && <Stat label="Complex tables" value={complexTables} />}
      {riskScore > 0 && <Stat label="Risk score" value={riskScore.toFixed(1)} />}
    </dl>
  );
}

function FormSemanticsDetails({ task }: { task: ReviewTask }) {
  const fieldCount = num(task, "field_count");
  const missingLabels = num(task, "missing_labels");
  const weakLabels = num(task, "weak_labels");
  if (!fieldCount) return null;
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      <Stat label="Form fields" value={fieldCount} />
      {missingLabels > 0 && <Stat label="Missing labels" value={missingLabels} />}
      {weakLabels > 0 && <Stat label="Weak labels" value={weakLabels} />}
    </dl>
  );
}

function FontFidelityDetails({ task }: { task: ReviewTask }) {
  const errors = num(task, "remaining_font_errors");
  const fonts = strList(task, "fonts_to_check");
  if (!errors && fonts.length === 0) return null;
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {errors > 0 && <Stat label="Font errors" value={errors} />}
      {fonts.length > 0 && <Stat label="Fonts to check" value={fonts.join(", ")} />}
    </dl>
  );
}

function MetadataPanel({ task }: { task: ReviewTask }) {
  switch (task.task_type) {
    case "content_fidelity":
      return <ContentFidelityDetails task={task} />;
    case "reading_order":
      return <ReadingOrderDetails task={task} />;
    case "table_semantics":
      return <TableSemanticsDetails task={task} />;
    case "form_semantics":
      return <FormSemanticsDetails task={task} />;
    case "font_text_fidelity":
      return <FontFidelityDetails task={task} />;
    default:
      return null;
  }
}

const TASK_TYPE_LABELS: Record<string, string> = {
  content_fidelity: "Content",
  reading_order: "Reading Order",
  table_semantics: "Tables",
  form_semantics: "Forms",
  font_text_fidelity: "Fonts",
};

const SUGGESTED_ACTION_LABELS: Record<string, string> = {
  mark_decorative: "Hide this content from screen readers (mark as decorative)",
  manual_only: "Review manually in an external tool like Adobe Acrobat Pro",
  artifact_if_decorative: "Hide this content from screen readers if it is decorative",
  actualtext_candidate: "Replace the garbled text with the correct readable text",
  font_map_candidate: "Fix the font encoding so characters display correctly",
};

function suggestedActionLabel(action: string): string {
  return SUGGESTED_ACTION_LABELS[action] ?? "Review manually";
}

/** Plain-language explanation of what was found, drawn from LLM analysis metadata. */
function WhatWeFound({ task }: { task: ReviewTask }) {
  const summary = task.metadata?.llm_summary as string | undefined;
  const confidence = task.metadata?.llm_confidence as string | undefined;
  const blocks = task.metadata?.flagged_blocks as Array<Record<string, unknown>> | undefined;

  if (!summary && !blocks?.length) return null;

  // Get the first block's suggested action for the recommendation
  const firstBlock = blocks?.[0];
  const suggestedAction = firstBlock?.suggested_action as string | undefined;

  return (
    <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-4 py-3 space-y-3">
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-1">
          What we found
        </h4>
        <p className="text-sm text-ink leading-relaxed">
          {summary}
        </p>
      </div>

      {suggestedAction && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-1">
            Suggested fix
          </h4>
          <p className="text-sm text-ink">
            {suggestedActionLabel(suggestedAction)}
          </p>
        </div>
      )}

      {confidence && (
        <p className="text-xs text-ink-muted">
          Confidence: <span className="font-medium">{confidence}</span>
        </p>
      )}
    </div>
  );
}

export default function FidelityIssueCard({ jobId, task, onResolve, resolving }: FidelityIssueCardProps) {
  const previewPages = previewPagesForTask(task);
  const pagesToCheck = pages(task);
  const isResolved = task.status === "resolved";
  const hasMetadata = (() => {
    switch (task.task_type) {
      case "content_fidelity":
        return num(task, "similarity") > 0 || num(task, "length_ratio") > 0 || num(task, "output_chars") > 0 || num(task, "containment") > 0 || num(task, "preservation") > 0;
      case "reading_order":
        return num(task, "hit_rate") > 0 || num(task, "order_rate") > 0;
      case "table_semantics":
        return num(task, "detected_tables") > 0 || num(task, "complex_tables") > 0;
      case "form_semantics":
        return num(task, "field_count") > 0;
      case "font_text_fidelity":
        return num(task, "remaining_font_errors") > 0 || strList(task, "fonts_to_check").length > 0;
      default:
        return false;
    }
  })();
  const typeLabel = TASK_TYPE_LABELS[task.task_type] ?? "Fidelity";

  const severityBadge = isResolved ? (
    <span className="rounded-full bg-success-light px-2 py-1 text-[11px] font-medium text-success">
      Resolved
    </span>
  ) : task.blocking ? (
    <span className="rounded-full bg-error-light px-2 py-1 text-[11px] font-medium text-error">
      Needs Fix
    </span>
  ) : (
    <span className="rounded-full bg-warning-light px-2 py-1 text-[11px] font-medium text-warning">
      Advisory
    </span>
  );

  return (
    <div className={`rounded-xl border p-5 transition-opacity ${
      isResolved
        ? "border-ink/6 bg-cream/50 opacity-60"
        : task.blocking
          ? "border-error/20 bg-error-light/5"
          : "border-ink/6 bg-cream"
    }`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{task.title}</h3>
          <p className="mt-1 text-sm text-ink-light">{task.detail}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          {severityBadge}
          <span className="rounded-full bg-paper-warm px-2 py-1 text-[11px] text-ink-muted">
            {typeLabel}
          </span>
        </div>
      </div>

      {previewPages.length > 0 && (
        <div className="mt-4">
          {previewPages.slice(0, 1).map((page) => {
            const url = pagePreviewUrl(jobId, page);
            return (
              <PreviewImage
                key={`${task.id}-preview-${page}`}
                src={url}
                href={url}
                alt={`Preview of page ${page}`}
                title={`Page ${page}`}
              />
            );
          })}
        </div>
      )}

      <WhatWeFound task={task} />

      {(hasMetadata || pagesToCheck.length > 0) && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3 space-y-3">
          {hasMetadata && <MetadataPanel task={task} />}
          {pagesToCheck.length > 0 && (
            <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Pages to check: {pagesToCheck.join(", ")}
            </p>
          )}
        </div>
      )}

      {!isResolved && onResolve && (
        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            onClick={() => onResolve(task.id)}
            disabled={resolving}
            className="
              inline-flex items-center gap-1.5 rounded-lg
              border border-ink/15 bg-white px-3.5 py-2
              text-sm font-medium text-ink
              hover:bg-cream hover:border-ink/25
              disabled:opacity-50 disabled:cursor-not-allowed
              transition-colors
            "
          >
            {resolving ? (
              <>
                <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round" />
                </svg>
                Resolving...
              </>
            ) : (
              "Mark as Resolved"
            )}
          </button>
          <span className="text-xs text-ink-muted">
            I've reviewed this and it's acceptable or I'll fix it externally.
          </span>
        </div>
      )}
    </div>
  );
}
