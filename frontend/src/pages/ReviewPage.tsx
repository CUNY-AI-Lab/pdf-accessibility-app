import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAltTexts,
  useApproveReview,
  useJob,
  useReviewTasks,
  useUpdateReviewTask,
  useUpdateAltText,
} from "../api/jobs";
import AltTextEditor from "../components/AltTextEditor";
import type { AltTextStatus, ReviewTask } from "../types";

type EvidenceField = {
  key: string;
  label: string;
  placeholder: string;
};

const TASK_EVIDENCE_FIELDS: Record<string, EvidenceField[]> = {
  reading_order: [
    {
      key: "verification_method",
      label: "Verification method",
      placeholder: "NVDA, exported text audit, or Acrobat reading order check",
    },
    {
      key: "pages_checked",
      label: "Pages checked",
      placeholder: "Pages 1-5 and all pages with sidebars or callouts",
    },
  ],
  font_text_fidelity: [
    {
      key: "assistive_tech",
      label: "Assistive technology",
      placeholder: "NVDA, VoiceOver, copy/paste audit, or text export",
    },
    {
      key: "sample_scope",
      label: "Sample scope",
      placeholder: "Cover page, formula pages, and a random spot check",
    },
  ],
  table_semantics: [
    {
      key: "tables_checked",
      label: "Tables checked",
      placeholder: "Tables on pages 2, 7, and 11",
    },
    {
      key: "verification_method",
      label: "Verification method",
      placeholder: "Screen-reader table navigation and tags inspection",
    },
  ],
  content_fidelity: [
    {
      key: "comparison_method",
      label: "Comparison method",
      placeholder: "Visible-vs-extracted text comparison or OCR spot check",
    },
    {
      key: "pages_checked",
      label: "Pages checked",
      placeholder: "First 3 pages and all pages with formulas or figures",
    },
  ],
  alt_text: [
    {
      key: "figures_checked",
      label: "Figures checked",
      placeholder: "Figures 1-4 and every chart or diagram",
    },
  ],
};

function evidenceFieldsForTask(taskType: string): EvidenceField[] {
  return TASK_EVIDENCE_FIELDS[taskType] ?? [];
}

function existingResolutionNote(task: ReviewTask): string {
  return typeof task.metadata?.resolution_note === "string"
    ? task.metadata.resolution_note
    : "";
}

function existingEvidenceForTask(task: ReviewTask): Record<string, string> {
  const rawEvidence = task.metadata?.evidence;
  if (!rawEvidence || typeof rawEvidence !== "object" || Array.isArray(rawEvidence)) {
    return {};
  }

  return Object.fromEntries(
    Object.entries(rawEvidence as Record<string, unknown>)
      .filter(([key]) => key.trim().length > 0)
      .map(([key, value]) => [key, typeof value === "string" ? value : String(value ?? "")]),
  );
}

function metadataEntriesForTask(task: ReviewTask): Array<[string, string]> {
  return Object.entries(task.metadata ?? {})
    .filter(([key]) => key !== "resolution_note" && key !== "evidence")
    .map(([key, value]) => {
      if (value && typeof value === "object") {
        return [key, JSON.stringify(value)];
      }
      return [key, String(value)];
    });
}

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const { data: altTexts, isLoading } = useAltTexts(id!, true);
  const { data: reviewTasks, isLoading: tasksLoading } = useReviewTasks(
    id!,
    job?.status === "needs_manual_review",
  );
  const updateAltText = useUpdateAltText(id!);
  const updateReviewTask = useUpdateReviewTask(id!);
  const approveReview = useApproveReview(id!);
  const [savingFigure, setSavingFigure] = useState<number | null>(null);
  const [savingTask, setSavingTask] = useState<number | null>(null);
  const [resolutionNotes, setResolutionNotes] = useState<Record<number, string>>({});
  const [resolutionEvidence, setResolutionEvidence] = useState<
    Record<number, Record<string, string>>
  >({});
  const isAltReview = job?.status === "awaiting_review";
  const isManualReview = job?.status === "needs_manual_review";

  const noteForTask = (taskId: number, fallback?: string) =>
    resolutionNotes[taskId] ?? fallback ?? "";

  const evidenceValueForTask = (
    task: ReviewTask,
    evidenceKey: string,
  ): string => {
    return (
      resolutionEvidence[task.id]?.[evidenceKey]
      ?? existingEvidenceForTask(task)[evidenceKey]
      ?? ""
    );
  };

  const collectEvidenceForTask = (
    task: ReviewTask,
  ): Record<string, string> | undefined => {
    const fields = evidenceFieldsForTask(task.task_type);
    if (fields.length === 0) {
      return undefined;
    }

    const evidence = Object.fromEntries(
      fields
        .map(({ key }) => [key, evidenceValueForTask(task, key).trim()] as const)
        .filter(([, value]) => value.length > 0),
    );

    return Object.keys(evidence).length > 0 ? evidence : undefined;
  };

  const missingEvidenceLabels = (task: ReviewTask): string[] =>
    evidenceFieldsForTask(task.task_type)
      .filter(({ key }) => evidenceValueForTask(task, key).trim().length === 0)
      .map(({ label }) => label);

  const canResolveTask = (task: ReviewTask): boolean =>
    noteForTask(task.id, existingResolutionNote(task)).trim().length > 0
    && missingEvidenceLabels(task).length === 0;

  const guidanceForTask = (taskType: string): string[] => {
    if (taskType === "reading_order") {
      return [
        "Read the document with a screen reader or exported text view.",
        "Check that headings, paragraphs, lists, and sidebars follow the intended order.",
      ];
    }
    if (taskType === "font_text_fidelity") {
      return [
        "Compare visible text against what copy/paste or a screen reader exposes.",
        "Pay attention to symbols, ligatures, math, and unusual fonts.",
      ];
    }
    if (taskType === "table_semantics") {
      return [
        "Verify header cells, spans, and reading order row by row.",
        "Confirm that assistive technology can identify the headers for each data cell.",
      ];
    }
    if (taskType === "content_fidelity") {
      return [
        "Check for missing text, duplicated text, or OCR drift.",
        "Compare the first pages and any pages with formulas or figures.",
      ];
    }
    if (taskType === "alt_text") {
      return [
        "Confirm the description matches the figure’s purpose in context.",
        "Reject generic or hallucinated descriptions.",
      ];
    }
    return [
      "Review this issue directly in the PDF and with assistive technology if needed.",
    ];
  };

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

  const handleUpdateTask = async (
    task: ReviewTask,
    status: "pending_review" | "resolved",
  ) => {
    setSavingTask(task.id);
    try {
      await updateReviewTask.mutateAsync({
        taskId: task.id,
        status,
        resolutionNote:
          status === "resolved"
            ? noteForTask(task.id, existingResolutionNote(task)).trim()
            : noteForTask(task.id, existingResolutionNote(task)),
        evidence: collectEvidenceForTask(task),
      });
    } finally {
      setSavingTask(null);
    }
  };

  const allReviewed =
    altTexts?.every((a) => a.status !== "pending_review") ?? false;
  const pendingCount =
    altTexts?.filter((a) => a.status === "pending_review").length ?? 0;
  const blockingValidationCount =
    reviewTasks?.filter((task) => task.blocking && task.source === "validation").length ?? 0;
  const pendingBlockingFidelityCount =
    reviewTasks?.filter(
      (task) =>
        task.blocking &&
        task.source !== "validation" &&
        task.status === "pending_review",
    ).length ?? 0;
  const finalizableManualReview =
    isManualReview &&
    blockingValidationCount === 0 &&
    pendingBlockingFidelityCount === 0;

  if (isLoading || tasksLoading) {
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
            {isManualReview ? "Review Accessibility Tasks" : "Review Alt Text"}
          </h1>
          <p className="text-sm text-ink-muted">
            {job?.original_filename}
            {isAltReview && altTexts && (
              <span>
                {" "}
                &middot; {altTexts.length} figure
                {altTexts.length !== 1 ? "s" : ""}
              </span>
            )}
            {isManualReview && reviewTasks && (
              <span>
                {" "}
                &middot; {reviewTasks.length} review task
                {reviewTasks.length !== 1 ? "s" : ""}
              </span>
            )}
          </p>
        </div>

        {/* Progress indicator */}
        {isAltReview && altTexts && altTexts.length > 0 && (
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
          {isAltReview
            ? "Review each figure's generated alt text. You can approve it as-is, edit it for accuracy, or mark purely decorative images. All figures must be reviewed before finalizing."
            : isManualReview
              ? "Review the blocking accessibility tasks before distributing this PDF. The fidelity gate found issues that need human judgment or manual remediation."
              : "This job is not currently waiting on review."}
        </p>
      </div>

      {/* Alt text editors */}
      {isAltReview && (
        altTexts && altTexts.length > 0 ? (
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
        )
      )}

      {isManualReview && reviewTasks && reviewTasks.length > 0 && (
        <div className="space-y-4 mb-8">
          {reviewTasks.map((task) => {
            const metadataEntries = metadataEntriesForTask(task);
            const evidenceFields = evidenceFieldsForTask(task.task_type);
            const resolutionNote = noteForTask(task.id, existingResolutionNote(task));
            const missingEvidence = missingEvidenceLabels(task);

            return (
              <div
                key={task.id}
                className="rounded-xl border border-ink/6 bg-cream p-5"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="font-display text-lg text-ink">{task.title}</h3>
                    <p className="text-sm text-ink-muted mt-1">{task.detail}</p>
                  </div>
                  <div className="flex flex-col items-end gap-2 shrink-0">
                    <span
                      className={`
                        text-[11px] px-2 py-1 rounded-full
                        ${
                          task.blocking
                            ? "bg-error-light text-error"
                            : "bg-warning-light text-warning"
                        }
                      `}
                    >
                      {task.blocking ? "Blocking" : "Advisory"}
                    </span>
                    <span className="text-xs text-ink-muted capitalize">
                      {task.severity} severity
                    </span>
                    <span className="text-xs text-ink-muted capitalize">
                      {task.source}
                    </span>
                  </div>
                </div>
                {metadataEntries.length > 0 && (
                  <p className="text-xs text-ink-muted mt-3 font-mono">
                    {metadataEntries
                      .map(([key, value]) => `${key}=${value}`)
                      .join(" | ")}
                  </p>
                )}
                <div className="mt-4 rounded-lg bg-paper-warm/60 px-3 py-3">
                  <p className="text-xs font-semibold text-ink mb-2">Review checklist</p>
                  <div className="space-y-1">
                    {guidanceForTask(task.task_type).map((item, index) => (
                      <p key={`${task.id}-guidance-${index}`} className="text-xs text-ink-muted">
                        {index + 1}. {item}
                      </p>
                    ))}
                  </div>
                </div>
                {task.source !== "validation" && (
                  <>
                    {evidenceFields.length > 0 && (
                      <div className="mt-4">
                        <p className="text-xs font-semibold text-ink mb-2">
                          Review evidence
                        </p>
                        <div className="grid gap-3 md:grid-cols-2">
                          {evidenceFields.map((field) => (
                            <label key={`${task.id}-${field.key}`} className="block">
                              <span className="block text-xs font-semibold text-ink mb-1">
                                {field.label}
                              </span>
                              <input
                                type="text"
                                value={evidenceValueForTask(task, field.key)}
                                onChange={(e) =>
                                  setResolutionEvidence((current) => ({
                                    ...current,
                                    [task.id]: {
                                      ...(current[task.id] ?? existingEvidenceForTask(task)),
                                      [field.key]: e.target.value,
                                    },
                                  }))
                                }
                                placeholder={field.placeholder}
                                className="
                                  w-full rounded-lg border border-ink/10 bg-white/70 px-3 py-2
                                  text-sm text-ink placeholder:text-ink-muted/70
                                  focus:outline-none focus:ring-2 focus:ring-accent/20
                                "
                              />
                            </label>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="mt-4">
                      <label className="block text-xs font-semibold text-ink mb-2">
                        Reviewer note
                      </label>
                      <textarea
                        value={resolutionNote}
                        onChange={(e) =>
                          setResolutionNotes((current) => ({
                            ...current,
                            [task.id]: e.target.value,
                          }))
                        }
                        rows={3}
                        placeholder="Record what you checked and how you verified it."
                        className="
                          w-full rounded-lg border border-ink/10 bg-white/70 px-3 py-2
                          text-sm text-ink placeholder:text-ink-muted/70
                          focus:outline-none focus:ring-2 focus:ring-accent/20
                        "
                      />
                    </div>
                    {task.status !== "resolved" && !canResolveTask(task) && (
                      <p className="mt-3 text-xs text-warning">
                        Required before marking reviewed:
                        {resolutionNote.trim().length === 0 ? " reviewer note" : ""}
                        {resolutionNote.trim().length === 0 && missingEvidence.length > 0
                          ? "; "
                          : " "}
                        {missingEvidence.length > 0
                          ? `${missingEvidence.join(", ")}`
                          : ""}
                      </p>
                    )}
                  </>
                )}
                <div className="mt-4 flex items-center gap-2">
                  {task.source === "validation" ? (
                    <span className="text-xs text-ink-muted bg-paper-warm px-2 py-1 rounded-full">
                      Read-only: requires actual PDF remediation
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() =>
                        handleUpdateTask(
                          task,
                          task.status === "resolved" ? "pending_review" : "resolved",
                        )
                      }
                      disabled={
                        savingTask === task.id
                        || updateReviewTask.isPending
                        || (task.status !== "resolved" && !canResolveTask(task))
                      }
                      className="
                        px-4 py-2 rounded-lg text-sm font-medium
                        bg-accent text-white
                        hover:bg-accent/90 transition-colors
                        disabled:opacity-50 disabled:cursor-not-allowed
                      "
                    >
                      {savingTask === task.id
                        ? "Saving..."
                        : task.status === "resolved"
                          ? "Reopen Task"
                          : "Mark Reviewed"}
                    </button>
                  )}
                  <span className="text-xs text-ink-muted capitalize">
                    status: {task.status.replaceAll("_", " ")}
                  </span>
                  {updateReviewTask.isError && savingTask === null && (
                    <span className="text-xs text-error">
                      {updateReviewTask.error?.message || "Failed to update task"}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {isManualReview && reviewTasks && reviewTasks.length === 0 && (
        <div className="text-center py-16 rounded-xl bg-cream border border-ink/6 mb-8">
          <h3 className="font-display text-lg text-ink mb-1">
            No review tasks recorded
          </h3>
          <p className="text-sm text-ink-muted">
            This job is flagged for manual review, but no task details were saved.
          </p>
        </div>
      )}

      {/* Approve & Finalize */}
      {isAltReview && altTexts && altTexts.length > 0 && (
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

      {isManualReview && (
        <div
          className="
            sticky bottom-6 rounded-xl bg-cream/95 backdrop-blur-sm
            border border-ink/8 shadow-lifted p-5
            flex items-center justify-between gap-4
          "
        >
          <div>
            <p className="text-sm font-medium text-ink">
              {blockingValidationCount > 0
                ? `${blockingValidationCount} validation task${blockingValidationCount !== 1 ? "s" : ""} still block release`
                : pendingBlockingFidelityCount > 0
                  ? `${pendingBlockingFidelityCount} blocking review task${pendingBlockingFidelityCount !== 1 ? "s" : ""} still need review`
                  : "Manual fidelity review can be finalized"}
            </p>
            <p className="text-xs text-ink-muted mt-0.5">
              {blockingValidationCount > 0
                ? "The PDF still has unresolved validation errors. Those cannot be cleared in-app."
                : finalizableManualReview
                  ? "All blocking fidelity tasks are resolved."
                  : "Resolve the remaining fidelity tasks to complete the manual review."}
            </p>
          </div>
          <button
            type="button"
            onClick={handleApproveAll}
            disabled={!finalizableManualReview || approveReview.isPending}
            className="
              px-6 py-3 rounded-xl
              bg-accent text-white font-semibold text-sm
              hover:bg-accent/90 shadow-sm
              transition-all duration-200
              disabled:opacity-40 disabled:cursor-not-allowed
            "
          >
            {approveReview.isPending ? "Finalizing..." : "Finalize Review"}
          </button>
        </div>
      )}
    </div>
  );
}
