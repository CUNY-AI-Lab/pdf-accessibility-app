import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useAltTexts,
  useApproveReview,
  useApplyFontActualText,
  useApplyFontActualTextBatch,
  useApplyFontUnicodeMapping,
  useJob,
  useReviewTasks,
  useSuggestReviewTask,
  useStructure,
  useUpdateStructure,
  useUpdateReviewTask,
  useUpdateAltText,
} from "../api/jobs";
import AltTextEditor from "../components/AltTextEditor";
import { CheckIcon, ChevronLeftIcon } from "../components/Icons";
import ReviewFinalizationBar from "../components/ReviewFinalizationBar";
import ReviewTaskCard from "../components/ReviewTaskCard";
import type { FontMutationState, StructureMutationState, TaskMutationState } from "../components/ReviewTaskCard";
import type { AltTextStatus, ReviewTask } from "../types";
import { pluralize } from "../utils/format";
import type { EditableStructureElement, FontReviewTarget, LlmSuggestion } from "./reviewHelpers";
import {
  actualTextKeyForTarget,
  applicableActualTextCandidates,
  ensureEditableStructure,
  evidenceFieldsForTask,
  existingEvidenceForTask,
  existingResolutionNote,
  structureElementsForPage,
  structurePages,
} from "./reviewHelpers";

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: job } = useJob(id!);
  const { data: altTexts, isLoading } = useAltTexts(id!, true);
  const { data: reviewTasks, isLoading: tasksLoading } = useReviewTasks(
    id!,
    job?.status === "needs_manual_review",
  );
  const { data: structure } = useStructure(id!, job?.status === "needs_manual_review");
  const updateAltText = useUpdateAltText(id!);
  const updateReviewTask = useUpdateReviewTask(id!);
  const suggestReviewTask = useSuggestReviewTask(id!);
  const applyFontActualText = useApplyFontActualText(id!);
  const applyFontActualTextBatch = useApplyFontActualTextBatch(id!);
  const applyFontUnicodeMapping = useApplyFontUnicodeMapping(id!);
  const updateStructure = useUpdateStructure(id!);
  const approveReview = useApproveReview(id!);
  const [savingFigure, setSavingFigure] = useState<number | null>(null);
  const [savingTask, setSavingTask] = useState<number | null>(null);
  const [suggestingTask, setSuggestingTask] = useState<number | null>(null);
  const [suggestErrorTask, setSuggestErrorTask] = useState<number | null>(null);
  const [applyingActualTextKey, setApplyingActualTextKey] = useState<string | null>(null);
  const [applyingActualTextBatchTaskId, setApplyingActualTextBatchTaskId] = useState<number | null>(null);
  const [applyingFontMapKey, setApplyingFontMapKey] = useState<string | null>(null);
  const [editingStructure, setEditingStructure] = useState<Record<string, unknown> | null>(null);
  const [structureHistory, setStructureHistory] = useState<Record<string, unknown>[]>([]);
  const [structureFuture, setStructureFuture] = useState<Record<string, unknown>[]>([]);
  const [selectedReadingOrderPage, setSelectedReadingOrderPage] = useState<number | null>(null);
  const [actualTextDrafts, setActualTextDrafts] = useState<Record<string, string>>({});
  const [resolutionNotes, setResolutionNotes] = useState<Record<number, string>>({});
  const [resolutionEvidence, setResolutionEvidence] = useState<
    Record<number, Record<string, string>>
  >({});
  const isAltReview = job?.status === "awaiting_review";
  const isManualReview = job?.status === "needs_manual_review";

  useEffect(() => {
    const normalized = ensureEditableStructure(structure);
    if (!normalized) {
      return;
    }
    setEditingStructure(normalized);
    setStructureHistory([]);
    setStructureFuture([]);
    setSelectedReadingOrderPage((current) => current ?? structurePages(normalized)[0] ?? null);
  }, [structure]);

  // ---------------------------------------------------------------------------
  // Derived values & helpers that depend on component state
  // ---------------------------------------------------------------------------

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

  // ---------------------------------------------------------------------------
  // Mutation handlers
  // ---------------------------------------------------------------------------

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

  const handleSuggestTask = async (task: ReviewTask) => {
    setSuggestingTask(task.id);
    setSuggestErrorTask(null);
    try {
      await suggestReviewTask.mutateAsync({ taskId: task.id });
      setSuggestErrorTask(null);
    } catch {
      setSuggestErrorTask(task.id);
    } finally {
      setSuggestingTask(null);
    }
  };

  // ---------------------------------------------------------------------------
  // Structure editing handlers
  // ---------------------------------------------------------------------------

  const applyStructureMutation = (
    mutator: (current: Record<string, unknown>) => Record<string, unknown>,
  ) => {
    setEditingStructure((current) => {
      if (!current) {
        return current;
      }
      const next = mutator(current);
      if (next === current || JSON.stringify(next) === JSON.stringify(current)) {
        return current;
      }
      setStructureHistory((history) => [...history, current]);
      setStructureFuture([]);
      return next;
    });
  };

  const handleUndoStructure = () => {
    setStructureHistory((history) => {
      if (history.length === 0 || !editingStructure) {
        return history;
      }
      const previous = history[history.length - 1];
      setStructureFuture((future) => [editingStructure, ...future]);
      setEditingStructure(previous);
      return history.slice(0, -1);
    });
  };

  const handleRedoStructure = () => {
    setStructureFuture((future) => {
      if (future.length === 0 || !editingStructure) {
        return future;
      }
      const [next, ...remaining] = future;
      setStructureHistory((history) => [...history, editingStructure]);
      setEditingStructure(next);
      return remaining;
    });
  };

  const moveReadingOrderElement = (pageNumber: number, reviewId: string, direction: -1 | 1) => {
    applyStructureMutation((current) => {
      if (!current || !Array.isArray(current.elements)) {
        return current;
      }
      const pageEntries = structureElementsForPage(current, pageNumber);
      const entryIndex = pageEntries.findIndex((entry) => entry.element.review_id === reviewId);
      const targetEntry = pageEntries[entryIndex + direction];
      if (entryIndex < 0 || !targetEntry) {
        return current;
      }

      const fromIndex = pageEntries[entryIndex].index;
      const toIndex = targetEntry.index;
      const nextElements = [...current.elements];
      const [movedElement] = nextElements.splice(fromIndex, 1);
      nextElements.splice(toIndex, 0, movedElement);

      return {
        ...current,
        elements: nextElements,
      };
    });
  };

  const toggleArtifactForElement = (reviewId: string) => {
    applyStructureMutation((current) => {
      if (!current || !Array.isArray(current.elements)) {
        return current;
      }
      const nextElements = current.elements.map((rawElement) => {
        if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
          return rawElement;
        }
        const element = rawElement as EditableStructureElement;
        if (element.review_id !== reviewId) {
          return rawElement;
        }

        if (element.type === "artifact") {
          const restoredType =
            typeof element._manual_original_type === "string" && element._manual_original_type.length > 0
              ? element._manual_original_type
              : "paragraph";
          const { _manual_original_type: _ignored, ...restored } = element;
          return {
            ...restored,
            type: restoredType,
          };
        }

        return {
          ...element,
          _manual_original_type:
            typeof element.type === "string" && element.type.length > 0
              ? element.type
              : "paragraph",
          type: "artifact",
        };
      });

      return {
        ...current,
        elements: nextElements,
      };
    });
  };

  const updateElementType = (reviewId: string, nextType: string) => {
    applyStructureMutation((current) => {
      if (!current || !Array.isArray(current.elements)) {
        return current;
      }
      const nextElements = current.elements.map((rawElement) => {
        if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
          return rawElement;
        }
        const element = rawElement as EditableStructureElement;
        if (element.review_id !== reviewId) {
          return rawElement;
        }

        if (nextType === "artifact") {
          return {
            ...element,
            _manual_original_type:
              typeof element.type === "string" && element.type.length > 0
                ? element.type
                : "paragraph",
            type: "artifact",
          };
        }

        const restored = {
          ...element,
          type: nextType,
        } as EditableStructureElement;

        if (nextType !== "heading") {
          delete restored.level;
        } else if (typeof restored.level !== "number" || restored.level < 1 || restored.level > 6) {
          restored.level = 1;
        }

        delete restored._manual_original_type;
        return restored;
      });

      return {
        ...current,
        elements: nextElements,
      };
    });
  };

  const updateHeadingLevel = (reviewId: string, nextLevel: number) => {
    applyStructureMutation((current) => {
      if (!current || !Array.isArray(current.elements)) {
        return current;
      }
      return {
        ...current,
        elements: current.elements.map((rawElement) => {
          if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
            return rawElement;
          }
          const element = rawElement as EditableStructureElement;
          if (element.review_id !== reviewId || element.type !== "heading") {
            return rawElement;
          }
          return {
            ...element,
            level: nextLevel,
          };
        }),
      };
    });
  };

  const resetReadingOrderPage = (pageNumber: number) => {
    const original = ensureEditableStructure(structure);
    if (!original || !Array.isArray(original.elements)) {
      return;
    }
    applyStructureMutation((current) => {
      if (!current || !Array.isArray(current.elements)) {
        return current;
      }
      const replacementEntries = structureElementsForPage(original, pageNumber);
      const replacementById = new Map(
        replacementEntries.map(({ element }) => [element.review_id, element]),
      );
      return {
        ...current,
        elements: current.elements.map((rawElement) => {
          if (!rawElement || typeof rawElement !== "object" || Array.isArray(rawElement)) {
            return rawElement;
          }
          const element = rawElement as EditableStructureElement;
          if ((typeof element.page === "number" ? element.page + 1 : null) !== pageNumber) {
            return rawElement;
          }
          return replacementById.get(element.review_id) ?? rawElement;
        }),
      };
    });
  };

  const handleSaveStructure = async () => {
    if (!editingStructure) {
      return;
    }
    try {
      await updateStructure.mutateAsync({ structure: editingStructure });
      navigate(`/jobs/${id}`);
    } catch {
      // error handled by mutation state
    }
  };

  // ---------------------------------------------------------------------------
  // Font remediation handlers
  // ---------------------------------------------------------------------------

  const handleApplyActualText = async (task: ReviewTask, target: FontReviewTarget) => {
    if (typeof target.page !== "number" || typeof target.operator_index !== "number") {
      return;
    }
    const draftKey = actualTextKeyForTarget(task.id, target);
    const actualText = (actualTextDrafts[draftKey] ?? "").trim();
    if (!actualText) {
      return;
    }

    setApplyingActualTextKey(draftKey);
    try {
      await applyFontActualText.mutateAsync({
        taskId: task.id,
        pageNumber: target.page,
        operatorIndex: target.operator_index,
        actualText,
      });
      navigate(`/jobs/${id}`);
    } finally {
      setApplyingActualTextKey(null);
    }
  };

  const handleApplyFontMap = async (task: ReviewTask, target: FontReviewTarget) => {
    if (typeof target.page !== "number" || typeof target.operator_index !== "number") {
      return;
    }
    const draftKey = actualTextKeyForTarget(task.id, target);
    const unicodeText = (actualTextDrafts[draftKey] ?? "").trim();
    if (!unicodeText) {
      return;
    }

    setApplyingFontMapKey(draftKey);
    try {
      await applyFontUnicodeMapping.mutateAsync({
        taskId: task.id,
        pageNumber: target.page,
        operatorIndex: target.operator_index,
        unicodeText,
      });
      navigate(`/jobs/${id}`);
    } finally {
      setApplyingFontMapKey(null);
    }
  };

  const handleApplySuggestedActualTextBatch = async (
    task: ReviewTask,
    llmSuggestion: LlmSuggestion | null,
    targets: FontReviewTarget[],
  ) => {
    const candidates = applicableActualTextCandidates(llmSuggestion, targets);
    if (candidates.length === 0) {
      return;
    }

    setApplyingActualTextBatchTaskId(task.id);
    try {
      await applyFontActualTextBatch.mutateAsync({
        taskId: task.id,
        targets: candidates.map((candidate) => ({
          pageNumber: candidate.page,
          operatorIndex: candidate.operator_index,
          actualText: candidate.proposed_actualtext,
        })),
      });
      navigate(`/jobs/${id}`);
    } finally {
      setApplyingActualTextBatchTaskId(null);
    }
  };

  const handleUseSuggestedActualText = (
    _task: ReviewTask,
    target: FontReviewTarget,
    proposedActualText: string,
  ) => {
    const draftKey = actualTextKeyForTarget(_task.id, target);
    setActualTextDrafts((current) => ({
      ...current,
      [draftKey]: proposedActualText,
    }));
  };

  const handleActualTextDraftChange = (key: string, value: string) => {
    setActualTextDrafts((current) => ({
      ...current,
      [key]: value,
    }));
  };

  const handleResolutionNoteChange = (taskId: number, value: string) => {
    setResolutionNotes((current) => ({
      ...current,
      [taskId]: value,
    }));
  };

  const handleEvidenceChange = (
    taskId: number,
    existingEvidence: Record<string, string>,
    key: string,
    value: string,
  ) => {
    setResolutionEvidence((current) => ({
      ...current,
      [taskId]: {
        ...(current[taskId] ?? existingEvidence),
        [key]: value,
      },
    }));
  };

  // ---------------------------------------------------------------------------
  // Derived counts
  // ---------------------------------------------------------------------------

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

  // ---------------------------------------------------------------------------
  // Loading state
  // ---------------------------------------------------------------------------

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

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

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
        <ChevronLeftIcon size={14} />
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
                &middot; {altTexts.length} {pluralize(altTexts.length, "figure")}
              </span>
            )}
            {isManualReview && reviewTasks && (
              <span>
                {" "}
                &middot; {reviewTasks.length} review {pluralize(reviewTasks.length, "task")}
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
              <CheckIcon size={20} />
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

      {/* Review task cards */}
      {isManualReview && reviewTasks && reviewTasks.length > 0 && (
        <div className="space-y-4 mb-8">
          {reviewTasks.map((task) => {
            const fontMutation: FontMutationState = {
              actualText: {
                isPending: applyFontActualText.isPending,
                error: applyFontActualText.isError ? applyFontActualText.error : null,
              },
              unicodeMapping: {
                isPending: applyFontUnicodeMapping.isPending,
                error: applyFontUnicodeMapping.isError ? applyFontUnicodeMapping.error : null,
              },
              actualTextBatch: {
                isPending: applyFontActualTextBatch.isPending,
                error: applyFontActualTextBatch.isError ? applyFontActualTextBatch.error : null,
                key: applyingActualTextBatchTaskId,
              },
              applyingActualTextKey,
              applyingFontMapKey,
            };
            const structureMutation: StructureMutationState = {
              update: {
                isPending: updateStructure.isPending,
                error: updateStructure.isError ? updateStructure.error : null,
              },
              historyLength: structureHistory.length,
              futureLength: structureFuture.length,
            };
            const taskMutation: TaskMutationState = {
              updateTask: {
                isPending: updateReviewTask.isPending,
                error: updateReviewTask.isError ? updateReviewTask.error : null,
              },
              suggestTask: {
                isPending: suggestReviewTask.isPending,
                error: suggestReviewTask.isError ? suggestReviewTask.error : null,
              },
              savingTask,
              suggestingTask,
              suggestErrorTask,
            };
            return (
              <ReviewTaskCard
                key={task.id}
                jobId={id!}
                task={task}
                resolutionNote={noteForTask(task.id, existingResolutionNote(task))}
                missingEvidence={missingEvidenceLabels(task)}
                canResolve={canResolveTask(task)}
                actualTextDrafts={actualTextDrafts}
                fontMutation={fontMutation}
                editingStructure={editingStructure}
                selectedReadingOrderPage={selectedReadingOrderPage}
                structureMutation={structureMutation}
                taskMutation={taskMutation}
                onResolutionNoteChange={handleResolutionNoteChange}
                onEvidenceChange={handleEvidenceChange}
                onActualTextDraftChange={handleActualTextDraftChange}
                onApplyActualText={handleApplyActualText}
                onApplyFontMap={handleApplyFontMap}
                onUseSuggestedActualText={handleUseSuggestedActualText}
                onApplySuggestedBatch={handleApplySuggestedActualTextBatch}
                onUpdateTask={handleUpdateTask}
                onSuggestTask={handleSuggestTask}
                onSelectReadingOrderPage={setSelectedReadingOrderPage}
                onUndoStructure={handleUndoStructure}
                onRedoStructure={handleRedoStructure}
                onResetReadingOrderPage={resetReadingOrderPage}
                onMoveElement={moveReadingOrderElement}
                onToggleArtifact={toggleArtifactForElement}
                onUpdateElementType={updateElementType}
                onUpdateHeadingLevel={updateHeadingLevel}
                onSaveStructure={handleSaveStructure}
                evidenceValueForTask={evidenceValueForTask}
              />
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

      {/* Finalization bars */}
      {isAltReview && altTexts && altTexts.length > 0 && (
        <ReviewFinalizationBar
          mode="alt_text"
          allReviewed={allReviewed}
          pendingCount={pendingCount}
          approving={approveReview.isPending}
          approveError={approveReview.isError ? approveReview.error : null}
          onApprove={handleApproveAll}
        />
      )}

      {isManualReview && (
        <ReviewFinalizationBar
          mode="manual_review"
          blockingValidationCount={blockingValidationCount}
          pendingBlockingFidelityCount={pendingBlockingFidelityCount}
          finalizable={finalizableManualReview}
          approving={approveReview.isPending}
          onApprove={handleApproveAll}
        />
      )}
    </div>
  );
}
