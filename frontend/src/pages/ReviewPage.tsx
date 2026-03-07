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

const LLM_SUGGESTION_TASK_TYPES = new Set(["font_text_fidelity", "reading_order"]);
const STRUCTURE_TYPE_OPTIONS = [
  { value: "paragraph", label: "Paragraph" },
  { value: "heading", label: "Heading" },
  { value: "list_item", label: "List Item" },
  { value: "code", label: "Code" },
  { value: "formula", label: "Formula" },
  { value: "artifact", label: "Artifact" },
] as const;

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
    .filter(
      ([key]) => key !== "resolution_note"
        && key !== "evidence"
        && key !== "llm_suggestion"
        && key !== "manual_actualtext_attempts"
        && key !== "font_rule_ids"
        && key !== "pages_to_check"
        && key !== "fonts_to_check"
        && key !== "font_review_targets",
    )
    .map(([key, value]) => {
      if (value && typeof value === "object") {
        return [key, JSON.stringify(value)];
      }
      return [key, String(value)];
    });
}

function manualActualTextAttempts(task: ReviewTask): Array<{
  page_number?: number;
  operator_index?: number;
  actual_text?: string;
  applied_at?: string;
  mode?: string;
}> {
  const value = task.metadata?.manual_actualtext_attempts;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is {
      page_number?: number;
      operator_index?: number;
      actual_text?: string;
      applied_at?: string;
      mode?: string;
    } => !!item && typeof item === "object" && !Array.isArray(item),
  );
}

function manualFontMappingAttempts(task: ReviewTask): Array<{
  page_number?: number;
  operator_index?: number;
  unicode_text?: string;
  font_base_name?: string;
  font_code_hex?: string;
  applied_at?: string;
}> {
  const value = task.metadata?.manual_font_mapping_attempts;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is {
      page_number?: number;
      operator_index?: number;
      unicode_text?: string;
      font_base_name?: string;
      font_code_hex?: string;
      applied_at?: string;
    } => !!item && typeof item === "object" && !Array.isArray(item),
  );
}

function stringListMetadata(task: ReviewTask, key: string): string[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "string" ? item.trim() : String(item ?? "").trim()))
    .filter((item) => item.length > 0);
}

function numberListMetadata(task: ReviewTask, key: string): number[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "number" ? item : Number(item)))
    .filter((item) => Number.isFinite(item) && item > 0);
}

type FontReviewTarget = {
  rule_id?: string;
  page?: number;
  font?: string;
  count?: number;
  content_stream_index?: number;
  operator_index?: number;
  sample_context?: string;
  decoded_text?: string;
  before_text?: string;
  after_text?: string;
  nearby_text?: string;
};

function fontReviewTargets(task: ReviewTask): FontReviewTarget[] {
  const value = task.metadata?.font_review_targets;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      rule_id: typeof item.rule_id === "string" ? item.rule_id : undefined,
      page: typeof item.page === "number" ? item.page : undefined,
      font: typeof item.font === "string" ? item.font : undefined,
      count: typeof item.count === "number" ? item.count : undefined,
      content_stream_index: typeof item.content_stream_index === "number" ? item.content_stream_index : undefined,
      operator_index: typeof item.operator_index === "number" ? item.operator_index : undefined,
      sample_context: typeof item.sample_context === "string" ? item.sample_context : undefined,
      decoded_text: typeof item.decoded_text === "string" ? item.decoded_text : undefined,
      before_text: typeof item.before_text === "string" ? item.before_text : undefined,
      after_text: typeof item.after_text === "string" ? item.after_text : undefined,
      nearby_text: typeof item.nearby_text === "string" ? item.nearby_text : undefined,
    }));
}

type LlmSuggestion = {
  summary?: string;
  confidence?: string;
  suggested_action?: string;
  reason?: string;
  generated_at?: string;
  model?: string;
  reviewer_checklist?: string[];
  review_focus?: Array<{
    page?: number;
    font?: string;
    operator_index?: number;
    rule_id?: string;
    visible_text_hypothesis?: string;
    is_likely_decorative?: boolean;
    recommended_reviewer_action?: string;
  }>;
  actualtext_candidates?: Array<{
    page?: number;
    operator_index?: number;
    font?: string;
    proposed_actualtext?: string;
    confidence?: string;
    reason?: string;
  }>;
};

function llmSuggestionForTask(task: ReviewTask): LlmSuggestion | null {
  const value = task.metadata?.llm_suggestion;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as LlmSuggestion;
}

function pagePreviewUrl(jobId: string, pageNumber: number): string {
  return `/api/jobs/${jobId}/pages/${pageNumber}/preview`;
}

function fontTargetPreviewUrl(
  jobId: string,
  taskId: number,
  target: FontReviewTarget,
): string | null {
  if (typeof target.page !== "number" || typeof target.operator_index !== "number") {
    return null;
  }
  const params = new URLSearchParams({
    page_number: String(target.page),
    operator_index: String(target.operator_index),
  });
  return `/api/jobs/${jobId}/review-tasks/${taskId}/font-target-preview?${params.toString()}`;
}

function actualTextCandidateForTarget(
  llmSuggestion: LlmSuggestion | null,
  target: FontReviewTarget,
) {
  const candidates = llmSuggestion?.actualtext_candidates;
  if (!Array.isArray(candidates)) {
    return null;
  }
  return (
    candidates.find((candidate) => {
      const candidatePage = typeof candidate.page === "number" ? candidate.page : null;
      const candidateOperator =
        typeof candidate.operator_index === "number" ? candidate.operator_index : null;
      const candidateFont =
        typeof candidate.font === "string" ? candidate.font.trim() : null;
      const targetFont = typeof target.font === "string" ? target.font.trim() : null;

      return candidatePage === target.page
        && candidateOperator === target.operator_index
        && (candidateFont === null || targetFont === null || candidateFont === targetFont);
    }) ?? null
  );
}

function applicableActualTextCandidates(
  llmSuggestion: LlmSuggestion | null,
  targets: FontReviewTarget[],
): Array<{
  page: number;
  operator_index: number;
  font?: string;
  proposed_actualtext: string;
  confidence?: string;
  reason?: string;
}> {
  const candidates = llmSuggestion?.actualtext_candidates;
  if (!Array.isArray(candidates)) {
    return [];
  }

  return candidates.filter((candidate) => {
    if (
      typeof candidate.page !== "number"
      || typeof candidate.operator_index !== "number"
      || typeof candidate.proposed_actualtext !== "string"
      || candidate.proposed_actualtext.trim().length === 0
    ) {
      return false;
    }
    return targets.some((target) => (
      target.page === candidate.page
      && target.operator_index === candidate.operator_index
      && (
        typeof candidate.font !== "string"
        || typeof target.font !== "string"
        || candidate.font.trim() === target.font.trim()
      )
    ));
  }) as Array<{
    page: number;
    operator_index: number;
    font?: string;
    proposed_actualtext: string;
    confidence?: string;
    reason?: string;
  }>;
}

function previewPagesForTask(task: ReviewTask, llmSuggestion: LlmSuggestion | null): number[] {
  const pages = new Set<number>();

  for (const page of numberListMetadata(task, "pages_to_check")) {
    if (page > 0) {
      pages.add(page);
    }
  }

  for (const target of fontReviewTargets(task)) {
    if (typeof target.page === "number" && target.page > 0) {
      pages.add(target.page);
    }
  }

  for (const item of llmSuggestion?.review_focus ?? []) {
    if (typeof item.page === "number" && item.page > 0) {
      pages.add(item.page);
    }
  }

  return Array.from(pages).sort((a, b) => a - b).slice(0, 3);
}

type EditableStructureElement = Record<string, unknown> & {
  review_id: string;
  page?: number;
  type?: string;
  text?: string;
};

function ensureEditableStructure(
  structure: Record<string, unknown> | undefined,
): Record<string, unknown> | null {
  if (!structure || typeof structure !== "object") {
    return null;
  }

  const rawElements = Array.isArray(structure.elements) ? structure.elements : [];
  const elements = rawElements
    .filter((element): element is Record<string, unknown> => !!element && typeof element === "object" && !Array.isArray(element))
    .map((element, index) => ({
      ...element,
      review_id:
        typeof element.review_id === "string" && element.review_id.trim().length > 0
          ? element.review_id
          : `review-${index}`,
    }));

  return {
    ...structure,
    elements,
  };
}

function structurePages(structure: Record<string, unknown> | null): number[] {
  const rawElements = Array.isArray(structure?.elements) ? structure.elements : [];
  const pages = new Set<number>();
  for (const element of rawElements) {
    if (!element || typeof element !== "object" || Array.isArray(element)) {
      continue;
    }
    const page = typeof element.page === "number" ? element.page + 1 : null;
    if (page && page > 0) {
      pages.add(page);
    }
  }
  return Array.from(pages).sort((a, b) => a - b);
}

function structureElementsForPage(
  structure: Record<string, unknown> | null,
  pageNumber: number,
): Array<{ element: EditableStructureElement; index: number }> {
  const rawElements = Array.isArray(structure?.elements) ? structure.elements : [];
  return rawElements
    .map((element, index) => ({ element, index }))
    .filter(
      (
        entry,
      ): entry is { element: EditableStructureElement; index: number } =>
        !!entry.element
        && typeof entry.element === "object"
        && !Array.isArray(entry.element)
        && typeof entry.element.review_id === "string"
        && (typeof entry.element.page === "number" ? entry.element.page + 1 : null) === pageNumber,
    );
}

function structureTypeLabel(type: string | undefined): string {
  const option = STRUCTURE_TYPE_OPTIONS.find((entry) => entry.value === type);
  return option?.label ?? String(type ?? "paragraph");
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

  const actualTextKeyForTarget = (taskId: number, target: FontReviewTarget): string =>
    `${taskId}:${target.page ?? "page"}:${target.operator_index ?? "op"}`;

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
    task: ReviewTask,
    target: FontReviewTarget,
    proposedActualText: string,
  ) => {
    const draftKey = actualTextKeyForTarget(task.id, target);
    setActualTextDrafts((current) => ({
      ...current,
      [draftKey]: proposedActualText,
    }));
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
            const pagesToCheck = numberListMetadata(task, "pages_to_check");
            const fontsToCheck = stringListMetadata(task, "fonts_to_check");
            const fontRuleIds = stringListMetadata(task, "font_rule_ids");
            const reviewTargets = fontReviewTargets(task);
            const actualTextAttempts = manualActualTextAttempts(task);
            const fontMappingAttempts = manualFontMappingAttempts(task);
            const llmSuggestion = llmSuggestionForTask(task);
            const suggestedActualTextCandidates = applicableActualTextCandidates(llmSuggestion, reviewTargets);
            const supportsSuggestion = LLM_SUGGESTION_TASK_TYPES.has(task.task_type);
            const suggestionGeneratedAt = llmSuggestion?.generated_at
              ? new Date(llmSuggestion.generated_at).toLocaleString()
              : null;
            const previewPages = previewPagesForTask(task, llmSuggestion);
            const editablePages = structurePages(editingStructure);
            const editorPage =
              selectedReadingOrderPage && editablePages.includes(selectedReadingOrderPage)
                ? selectedReadingOrderPage
                : previewPages[0] ?? editablePages[0] ?? null;
            const pageElements =
              task.task_type === "reading_order" && editorPage
                ? structureElementsForPage(editingStructure, editorPage)
                : [];
            const hasUnsavedStructureEdits = structureHistory.length > 0;

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
                {(pagesToCheck.length > 0 || fontsToCheck.length > 0 || fontRuleIds.length > 0) && (
                  <div className="mt-4 rounded-lg border border-ink/8 bg-white/60 px-3 py-3">
                    <p className="text-xs font-semibold text-ink mb-2">Review focus</p>
                    <div className="grid gap-3 md:grid-cols-3">
                      {pagesToCheck.length > 0 && (
                        <div>
                          <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                            Pages
                          </p>
                          <p className="text-sm text-ink mt-1">
                            {pagesToCheck.join(", ")}
                          </p>
                        </div>
                      )}
                      {fontsToCheck.length > 0 && (
                        <div>
                          <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                            Fonts
                          </p>
                          <p className="text-sm text-ink mt-1">
                            {fontsToCheck.join(", ")}
                          </p>
                        </div>
                      )}
                      {fontRuleIds.length > 0 && (
                        <div>
                          <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                            Rules
                          </p>
                          <p className="text-sm text-ink mt-1 break-words">
                            {fontRuleIds.join(", ")}
                          </p>
                        </div>
                      )}
                    </div>
                  </div>
                )}
                {previewPages.length > 0 && (
                  <div className="mt-4">
                    <p className="text-xs font-semibold text-ink mb-2">Relevant pages</p>
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                      {previewPages.map((pageNumber) => (
                        <a
                          key={`${task.id}-page-${pageNumber}`}
                          href={pagePreviewUrl(id!, pageNumber)}
                          target="_blank"
                          rel="noreferrer"
                          className="block rounded-lg border border-ink/8 bg-white/70 p-2 no-underline"
                        >
                          <p className="text-xs font-semibold text-ink mb-2">
                            Page {pageNumber}
                          </p>
                          <img
                            src={pagePreviewUrl(id!, pageNumber)}
                            alt={`Preview of page ${pageNumber}`}
                            loading="lazy"
                            className="w-full rounded-md border border-ink/6 bg-paper-warm object-cover"
                          />
                        </a>
                      ))}
                    </div>
                  </div>
                )}
                {reviewTargets.length > 0 && (
                  <div className="mt-4 rounded-lg border border-ink/8 bg-paper-warm/70 px-3 py-3">
                    <p className="text-xs font-semibold text-ink mb-2">Targeted findings</p>
                    <div className="space-y-2">
                      {reviewTargets.map((target, index) => (
                        <div
                          key={`${task.id}-target-${index}`}
                          className="rounded-lg bg-white/70 px-3 py-2"
                        >
                          {(() => {
                            const actualtextCandidate = actualTextCandidateForTarget(llmSuggestion, target);
                            const targetPreview = fontTargetPreviewUrl(id!, task.id, target);
                            return (
                              <>
                          <p className="text-sm text-ink">
                            {target.page ? `Page ${target.page}` : "Page unknown"}
                            {target.font ? ` · ${target.font}` : ""}
                            {target.rule_id ? ` · ${target.rule_id}` : ""}
                            {typeof target.operator_index === "number" ? ` · operator ${target.operator_index}` : ""}
                            {typeof target.count === "number" ? ` · ${target.count} occurrence${target.count === 1 ? "" : "s"}` : ""}
                          </p>
                          {target.sample_context && (
                            <p className="mt-1 text-xs font-mono text-ink-muted break-all">
                              {target.sample_context}
                            </p>
                          )}
                          {(target.decoded_text || target.before_text || target.after_text || target.nearby_text) && (
                            <div className="mt-2 rounded-lg bg-paper-warm/70 px-3 py-2">
                              <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                                Local text context
                              </p>
                              {target.decoded_text && (
                                <p className="mt-1 text-xs text-ink-muted">
                                  Target decode: {target.decoded_text}
                                </p>
                              )}
                              {target.before_text && (
                                <p className="mt-1 text-xs text-ink-muted">
                                  Before: {target.before_text}
                                </p>
                              )}
                              {target.after_text && (
                                <p className="mt-1 text-xs text-ink-muted">
                                  After: {target.after_text}
                                </p>
                              )}
                              {!target.before_text && !target.after_text && target.nearby_text && (
                                <p className="mt-1 text-xs text-ink-muted">
                                  Nearby: {target.nearby_text}
                                </p>
                              )}
                            </div>
                          )}
                          {targetPreview && (
                            <div className="mt-2">
                              <a
                                href={targetPreview}
                                target="_blank"
                                rel="noreferrer"
                                className="block rounded-lg border border-ink/8 bg-paper-warm/60 p-2 no-underline"
                              >
                                <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted mb-2">
                                  Target preview
                                </p>
                                <img
                                  src={targetPreview}
                                  alt={`Preview for page ${target.page} operator ${target.operator_index}`}
                                  loading="lazy"
                                  className="w-full rounded-md border border-ink/6 bg-paper-warm object-cover"
                                />
                              </a>
                            </div>
                          )}
                          {actualtextCandidate && typeof actualtextCandidate.proposed_actualtext === "string" && actualtextCandidate.proposed_actualtext.trim().length > 0 && (
                            <div className="mt-3 rounded-lg border border-accent-light bg-accent-glow/60 px-3 py-2">
                              <p className="text-xs font-semibold text-ink">
                                Gemini `ActualText` suggestion
                              </p>
                              <p className="mt-1 text-sm text-ink break-words">
                                {actualtextCandidate.proposed_actualtext}
                              </p>
                              <p className="mt-1 text-xs text-ink-muted">
                                {actualtextCandidate.confidence ? `${actualtextCandidate.confidence} confidence` : "Confidence not provided"}
                                {actualtextCandidate.reason ? ` · ${actualtextCandidate.reason}` : ""}
                              </p>
                              <div className="mt-2">
                                <button
                                  type="button"
                                  onClick={() => handleUseSuggestedActualText(task, target, actualtextCandidate.proposed_actualtext ?? "")}
                                  className="
                                    px-3 py-2 rounded-lg text-xs font-medium
                                    bg-white text-ink border border-ink/10
                                    hover:border-accent-light transition-colors
                                  "
                                >
                                  Use suggestion
                                </button>
                              </div>
                            </div>
                          )}
                          {task.task_type === "font_text_fidelity"
                            && typeof target.page === "number"
                            && typeof target.operator_index === "number" && (
                              <>
                                <div className="mt-3 flex flex-col gap-2 md:flex-row md:items-center">
                                  <input
                                    type="text"
                                    value={actualTextDrafts[actualTextKeyForTarget(task.id, target)] ?? ""}
                                    onChange={(e) =>
                                      setActualTextDrafts((current) => ({
                                        ...current,
                                        [actualTextKeyForTarget(task.id, target)]: e.target.value,
                                      }))
                                    }
                                    placeholder="Correct text for this visible glyph or symbol"
                                    className="
                                      flex-1 rounded-lg border border-ink/10 bg-white/80 px-3 py-2
                                      text-sm text-ink placeholder:text-ink-muted/70
                                      focus:outline-none focus:ring-2 focus:ring-accent/20
                                    "
                                  />
                                  <button
                                    type="button"
                                    onClick={() => handleApplyActualText(task, target)}
                                    disabled={
                                      applyingActualTextKey === actualTextKeyForTarget(task.id, target)
                                      || applyFontActualText.isPending
                                      || (actualTextDrafts[actualTextKeyForTarget(task.id, target)] ?? "").trim().length === 0
                                    }
                                    className="
                                      px-3 py-2 rounded-lg text-xs font-medium
                                      bg-accent text-white
                                      hover:bg-accent/90 transition-colors
                                      disabled:opacity-50 disabled:cursor-not-allowed
                                    "
                                  >
                                    {applyingActualTextKey === actualTextKeyForTarget(task.id, target)
                                      ? "Applying..."
                                      : "Apply ActualText"}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => handleApplyFontMap(task, target)}
                                    disabled={
                                      applyingFontMapKey === actualTextKeyForTarget(task.id, target)
                                      || applyFontUnicodeMapping.isPending
                                      || (actualTextDrafts[actualTextKeyForTarget(task.id, target)] ?? "").trim().length === 0
                                    }
                                    className="
                                      px-3 py-2 rounded-lg text-xs font-medium
                                      bg-white text-ink border border-ink/10
                                      hover:border-accent-light transition-colors
                                      disabled:opacity-50 disabled:cursor-not-allowed
                                    "
                                  >
                                    {applyingFontMapKey === actualTextKeyForTarget(task.id, target)
                                      ? "Applying..."
                                      : "Apply Font Map"}
                                  </button>
                                </div>
                                <p className="mt-2 text-[11px] text-ink-muted">
                                  `ActualText` changes this one target for assistive output. `Font Map` writes a `ToUnicode` override for this font/code and affects every matching use in the font.
                                </p>
                              </>
                            )}
                              </>
                            );
                          })()}
                        </div>
                      ))}
                    </div>
                    {applyFontActualText.isError && (
                      <p className="mt-3 text-xs text-error">
                        {applyFontActualText.error?.message || "Failed to apply ActualText remediation"}
                      </p>
                    )}
                    {applyFontUnicodeMapping.isError && (
                      <p className="mt-3 text-xs text-error">
                        {applyFontUnicodeMapping.error?.message || "Failed to apply font-map remediation"}
                      </p>
                    )}
                  </div>
                )}
                {actualTextAttempts.length > 0 && (
                  <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
                    <p className="text-xs font-semibold text-ink mb-2">
                      Applied `ActualText` attempts
                    </p>
                    <div className="space-y-2">
                      {actualTextAttempts.map((attempt, index) => (
                        <div
                          key={`${task.id}-actualtext-attempt-${index}`}
                          className="rounded-lg bg-paper-warm/70 px-3 py-2"
                        >
                          <p className="text-sm text-ink">
                            {typeof attempt.page_number === "number" ? `Page ${attempt.page_number}` : "Page unknown"}
                            {typeof attempt.operator_index === "number" ? ` · operator ${attempt.operator_index}` : ""}
                            {attempt.mode ? ` · ${attempt.mode}` : ""}
                          </p>
                          {attempt.actual_text && (
                            <p className="mt-1 text-sm text-ink break-words">
                              {attempt.actual_text}
                            </p>
                          )}
                          {attempt.applied_at && (
                            <p className="mt-1 text-xs text-ink-muted">
                              Applied {new Date(attempt.applied_at).toLocaleString()}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {fontMappingAttempts.length > 0 && (
                  <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
                    <p className="text-xs font-semibold text-ink mb-2">
                      Applied font-map attempts
                    </p>
                    <div className="space-y-2">
                      {fontMappingAttempts.map((attempt, index) => (
                        <div
                          key={`${task.id}-fontmap-attempt-${index}`}
                          className="rounded-lg bg-paper-warm/70 px-3 py-2"
                        >
                          <p className="text-sm text-ink">
                            {typeof attempt.page_number === "number" ? `Page ${attempt.page_number}` : "Page unknown"}
                            {typeof attempt.operator_index === "number" ? ` · operator ${attempt.operator_index}` : ""}
                            {attempt.font_base_name ? ` · ${attempt.font_base_name}` : ""}
                            {attempt.font_code_hex ? ` · code ${attempt.font_code_hex}` : ""}
                          </p>
                          {attempt.unicode_text && (
                            <p className="mt-1 text-sm text-ink break-words">
                              {attempt.unicode_text}
                            </p>
                          )}
                          {attempt.applied_at && (
                            <p className="mt-1 text-xs text-ink-muted">
                              Applied {new Date(attempt.applied_at).toLocaleString()}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
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
                {task.task_type === "reading_order" && (
                  <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-xs font-semibold text-ink">
                          Reading order editor
                        </p>
                        <p className="text-xs text-ink-muted mt-1">
                          Reorder elements on a page or artifact repeated side material, then rerun tagging and validation.
                        </p>
                      </div>
                      {editablePages.length > 0 && (
                        <label className="text-xs text-ink-muted">
                          <span className="block mb-1">Page</span>
                          <select
                            value={editorPage ?? ""}
                            onChange={(e) => setSelectedReadingOrderPage(Number(e.target.value))}
                            className="
                              rounded-lg border border-ink/10 bg-white px-3 py-2 text-sm text-ink
                              focus:outline-none focus:ring-2 focus:ring-accent/20
                            "
                          >
                            {editablePages.map((pageNumber) => (
                              <option key={`${task.id}-editor-page-${pageNumber}`} value={pageNumber}>
                                Page {pageNumber}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                    </div>
                    {editorPage && pageElements.length > 0 ? (
                      <div className="mt-4 space-y-2">
                        <div className="flex flex-wrap items-center gap-2 pb-2">
                          <button
                            type="button"
                            onClick={handleUndoStructure}
                            disabled={structureHistory.length === 0 || updateStructure.isPending}
                            className="
                              px-3 py-2 rounded-lg text-xs font-medium
                              bg-white border border-ink/10 text-ink
                              disabled:opacity-40 disabled:cursor-not-allowed
                            "
                          >
                            Undo
                          </button>
                          <button
                            type="button"
                            onClick={handleRedoStructure}
                            disabled={structureFuture.length === 0 || updateStructure.isPending}
                            className="
                              px-3 py-2 rounded-lg text-xs font-medium
                              bg-white border border-ink/10 text-ink
                              disabled:opacity-40 disabled:cursor-not-allowed
                            "
                          >
                            Redo
                          </button>
                          <button
                            type="button"
                            onClick={() => resetReadingOrderPage(editorPage)}
                            disabled={updateStructure.isPending}
                            className="
                              px-3 py-2 rounded-lg text-xs font-medium
                              bg-white border border-ink/10 text-ink
                              disabled:opacity-40 disabled:cursor-not-allowed
                            "
                          >
                            Reset Page
                          </button>
                        </div>
                        {pageElements.map(({ element }, index) => (
                          <div
                            key={element.review_id}
                            className="rounded-lg border border-ink/8 bg-paper-warm/50 px-3 py-3"
                          >
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0">
                                <p className="text-xs text-ink-muted">
                                  {index + 1}. {structureTypeLabel(typeof element.type === "string" ? element.type : "paragraph")}
                                </p>
                                <p className="text-sm text-ink mt-1 break-words">
                                  {typeof element.text === "string" && element.text.trim().length > 0
                                    ? element.text
                                    : "[non-text element]"}
                                </p>
                              </div>
                              <div className="flex items-center gap-2 shrink-0">
                                <label className="text-xs text-ink-muted">
                                  <span className="sr-only">Element type</span>
                                  <select
                                    value={typeof element.type === "string" ? element.type : "paragraph"}
                                    onChange={(e) => updateElementType(element.review_id, e.target.value)}
                                    disabled={updateStructure.isPending}
                                    className="
                                      rounded-lg border border-ink/10 bg-white px-3 py-2 text-xs text-ink
                                      focus:outline-none focus:ring-2 focus:ring-accent/20
                                    "
                                  >
                                    {STRUCTURE_TYPE_OPTIONS.map((option) => (
                                      <option key={`${element.review_id}-${option.value}`} value={option.value}>
                                        {option.label}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                {element.type === "heading" && (
                                  <label className="text-xs text-ink-muted">
                                    <span className="sr-only">Heading level</span>
                                    <select
                                      value={typeof element.level === "number" ? element.level : 1}
                                      onChange={(e) => updateHeadingLevel(element.review_id, Number(e.target.value))}
                                      disabled={updateStructure.isPending}
                                      className="
                                        rounded-lg border border-ink/10 bg-white px-3 py-2 text-xs text-ink
                                        focus:outline-none focus:ring-2 focus:ring-accent/20
                                      "
                                    >
                                      {[1, 2, 3, 4, 5, 6].map((level) => (
                                        <option key={`${element.review_id}-h${level}`} value={level}>
                                          H{level}
                                        </option>
                                      ))}
                                    </select>
                                  </label>
                                )}
                                <button
                                  type="button"
                                  onClick={() => moveReadingOrderElement(editorPage, element.review_id, -1)}
                                  disabled={index === 0 || updateStructure.isPending}
                                  className="
                                    px-3 py-2 rounded-lg text-xs font-medium
                                    bg-white border border-ink/10 text-ink
                                    disabled:opacity-40 disabled:cursor-not-allowed
                                  "
                                >
                                  Move Up
                                </button>
                                <button
                                  type="button"
                                  onClick={() => moveReadingOrderElement(editorPage, element.review_id, 1)}
                                  disabled={index === pageElements.length - 1 || updateStructure.isPending}
                                  className="
                                    px-3 py-2 rounded-lg text-xs font-medium
                                    bg-white border border-ink/10 text-ink
                                    disabled:opacity-40 disabled:cursor-not-allowed
                                  "
                                >
                                  Move Down
                                </button>
                                <button
                                  type="button"
                                  onClick={() => toggleArtifactForElement(element.review_id)}
                                  disabled={updateStructure.isPending}
                                  className="
                                    px-3 py-2 rounded-lg text-xs font-medium
                                    bg-white border border-ink/10 text-ink
                                    disabled:opacity-40 disabled:cursor-not-allowed
                                  "
                                >
                                  {element.type === "artifact" ? "Restore Type" : "Mark Artifact"}
                                </button>
                              </div>
                            </div>
                          </div>
                        ))}
                        <div className="flex items-center justify-between gap-3 pt-2">
                          <p className="text-xs text-ink-muted">
                            {hasUnsavedStructureEdits
                              ? "Saving reruns tagging, validation, and fidelity on the edited structure."
                              : "No unsaved structure edits yet."}
                          </p>
                          <button
                            type="button"
                            onClick={handleSaveStructure}
                            disabled={updateStructure.isPending || !hasUnsavedStructureEdits}
                            className="
                              px-4 py-2 rounded-lg text-sm font-medium
                              bg-accent text-white
                              hover:bg-accent/90 transition-colors
                              disabled:opacity-50 disabled:cursor-not-allowed
                            "
                          >
                            {updateStructure.isPending ? "Reprocessing..." : "Save Structure Edits"}
                          </button>
                        </div>
                        {updateStructure.isError && (
                          <p className="text-xs text-error">
                            {updateStructure.error?.message || "Failed to save structure edits"}
                          </p>
                        )}
                      </div>
                    ) : (
                      <p className="mt-3 text-xs text-ink-muted">
                        No editable structure elements are available for this page yet.
                      </p>
                    )}
                  </div>
                )}
                {(supportsSuggestion || llmSuggestion) && (
                  <div className="mt-4 rounded-lg border border-accent-light bg-accent-glow/60 px-3 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-xs font-semibold text-ink">Gemini suggestion</p>
                        <p className="text-xs text-ink-muted mt-1">
                          Proposal only. Review it, then apply remediation or manual verification yourself.
                        </p>
                      </div>
                      {supportsSuggestion && (
                        <button
                          type="button"
                          onClick={() => handleSuggestTask(task)}
                          disabled={suggestingTask === task.id || suggestReviewTask.isPending}
                          className="
                            px-3 py-2 rounded-lg text-xs font-medium
                            bg-accent text-white
                            hover:bg-accent/90 transition-colors
                            disabled:opacity-50 disabled:cursor-not-allowed
                          "
                        >
                          {suggestingTask === task.id ? "Analyzing..." : llmSuggestion ? "Refresh Suggestion" : "Suggest Fix"}
                        </button>
                      )}
                    </div>
                    {llmSuggestion ? (
                      <div className="mt-3 space-y-3">
                        <div className="grid gap-3 md:grid-cols-3">
                          <div className="rounded-lg bg-white/70 px-3 py-2">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                              Action
                            </p>
                            <p className="mt-1 text-sm text-ink break-words">
                              {String(llmSuggestion.suggested_action ?? "manual_only").replaceAll("_", " ")}
                            </p>
                          </div>
                          <div className="rounded-lg bg-white/70 px-3 py-2">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                              Confidence
                            </p>
                            <p className="mt-1 text-sm text-ink capitalize">
                              {String(llmSuggestion.confidence ?? "unknown")}
                            </p>
                          </div>
                          <div className="rounded-lg bg-white/70 px-3 py-2">
                            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                              Model
                            </p>
                            <p className="mt-1 text-sm text-ink break-words">
                              {llmSuggestion.model ?? "unknown"}
                            </p>
                          </div>
                        </div>
                        {llmSuggestion.summary && (
                          <p className="text-sm text-ink">{llmSuggestion.summary}</p>
                        )}
                        {llmSuggestion.reason && (
                          <p className="text-xs text-ink-muted">{llmSuggestion.reason}</p>
                        )}
                        {task.task_type === "font_text_fidelity" && suggestedActualTextCandidates.length > 0 && (
                          <div className="rounded-lg border border-accent-light bg-white/70 px-3 py-3">
                            <div className="flex items-start justify-between gap-3">
                              <div>
                                <p className="text-xs font-semibold text-ink">
                                  Suggested `ActualText` batch
                                </p>
                                <p className="mt-1 text-xs text-ink-muted">
                                  Reviewer-approved batch apply. This rewrites all suggested localized targets and reruns validation once.
                                </p>
                              </div>
                              <button
                                type="button"
                                onClick={() => handleApplySuggestedActualTextBatch(task, llmSuggestion, reviewTargets)}
                                disabled={applyingActualTextBatchTaskId === task.id || applyFontActualTextBatch.isPending}
                                className="
                                  px-3 py-2 rounded-lg text-xs font-medium
                                  bg-accent text-white
                                  hover:bg-accent/90 transition-colors
                                  disabled:opacity-50 disabled:cursor-not-allowed
                                "
                              >
                                {applyingActualTextBatchTaskId === task.id ? "Applying..." : "Apply Suggested Batch"}
                              </button>
                            </div>
                            <div className="mt-3 space-y-2">
                              {suggestedActualTextCandidates.map((candidate, index) => (
                                <div
                                  key={`${task.id}-batch-candidate-${index}`}
                                  className="rounded-lg bg-paper-warm/70 px-3 py-2"
                                >
                                  <p className="text-sm text-ink">
                                    Page {candidate.page} · operator {candidate.operator_index}
                                    {candidate.font ? ` · ${candidate.font}` : ""}
                                  </p>
                                  <p className="mt-1 text-sm text-ink break-words">
                                    {candidate.proposed_actualtext}
                                  </p>
                                  <p className="mt-1 text-xs text-ink-muted">
                                    {candidate.confidence ? `${candidate.confidence} confidence` : "Confidence not provided"}
                                    {candidate.reason ? ` · ${candidate.reason}` : ""}
                                  </p>
                                </div>
                              ))}
                            </div>
                            {applyFontActualTextBatch.isError && applyingActualTextBatchTaskId === task.id && (
                              <p className="mt-3 text-xs text-error">
                                {applyFontActualTextBatch.error?.message || "Failed to apply ActualText batch remediation"}
                              </p>
                            )}
                          </div>
                        )}
                        {llmSuggestion.review_focus && llmSuggestion.review_focus.length > 0 && (
                          <div>
                            <p className="text-xs font-semibold text-ink mb-2">
                              Suggested review focus
                            </p>
                            <div className="space-y-2">
                              {llmSuggestion.review_focus.map((item, index) => (
                                <div
                                  key={`${task.id}-focus-${index}`}
                                  className="rounded-lg bg-white/70 px-3 py-2"
                                >
                                  <p className="text-sm text-ink">
                                    {item.page ? `Page ${item.page}` : "Page unknown"}
                                    {item.font ? ` · ${item.font}` : ""}
                                    {item.rule_id ? ` · ${item.rule_id}` : ""}
                                  </p>
                                  {item.visible_text_hypothesis && (
                                    <p className="mt-1 text-xs text-ink-muted">
                                      Visible text hypothesis: {item.visible_text_hypothesis}
                                    </p>
                                  )}
                                  {typeof item.is_likely_decorative === "boolean" && (
                                    <p className="mt-1 text-xs text-ink-muted">
                                      Likely decorative: {item.is_likely_decorative ? "yes" : "no"}
                                    </p>
                                  )}
                                  {item.recommended_reviewer_action && (
                                    <p className="mt-1 text-xs text-ink-muted">
                                      Reviewer action: {item.recommended_reviewer_action}
                                    </p>
                                  )}
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {llmSuggestion.reviewer_checklist && llmSuggestion.reviewer_checklist.length > 0 && (
                          <div>
                            <p className="text-xs font-semibold text-ink mb-2">
                              Suggested checklist
                            </p>
                            <div className="space-y-1">
                              {llmSuggestion.reviewer_checklist.map((item, index) => (
                                <p key={`${task.id}-llm-check-${index}`} className="text-xs text-ink-muted">
                                  {index + 1}. {item}
                                </p>
                              ))}
                            </div>
                          </div>
                        )}
                        {suggestionGeneratedAt && (
                          <p className="text-[11px] text-ink-muted">
                            Generated {suggestionGeneratedAt}
                          </p>
                        )}
                      </div>
                    ) : (
                      <p className="mt-3 text-xs text-ink-muted">
                        No suggestion generated yet.
                      </p>
                    )}
                    {suggestReviewTask.isError && suggestErrorTask === task.id && (
                      <p className="mt-3 text-xs text-error">
                        {suggestReviewTask.error?.message || "Failed to generate suggestion"}
                      </p>
                    )}
                  </div>
                )}
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
