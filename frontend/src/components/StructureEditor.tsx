import type { EditableStructureElement, ReadingOrderTextHint } from "../pages/reviewHelpers";
import {
  STRUCTURE_TYPE_OPTIONS,
  structureTypeLabel,
} from "../pages/reviewHelpers";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface StructureEditorProps {
  taskId: number;
  editablePages: number[];
  editorPage: number | null;
  editorPagePreviewUrl?: string | null;
  pageElements: Array<{ element: EditableStructureElement; index: number }>;
  readableTextHints?: ReadingOrderTextHint[];
  hasUnsavedEdits: boolean;
  structureHistoryLength: number;
  structureFutureLength: number;
  updateStructurePending: boolean;
  updateStructureError: Error | null;
  onSelectPage: (page: number) => void;
  onUndo: () => void;
  onRedo: () => void;
  onResetPage: (page: number) => void;
  onMoveElement: (page: number, reviewId: string, direction: -1 | 1) => void;
  onToggleArtifact: (reviewId: string) => void;
  onUpdateElementType: (reviewId: string, nextType: string) => void;
  onUpdateHeadingLevel: (reviewId: string, level: number) => void;
  onSave: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function StructureEditor({
  taskId,
  editablePages,
  editorPage,
  editorPagePreviewUrl,
  pageElements,
  readableTextHints = [],
  hasUnsavedEdits,
  structureHistoryLength,
  structureFutureLength,
  updateStructurePending,
  updateStructureError,
  onSelectPage,
  onUndo,
  onRedo,
  onResetPage,
  onMoveElement,
  onToggleArtifact,
  onUpdateElementType,
  onUpdateHeadingLevel,
  onSave,
}: StructureEditorProps) {
  const hintsByReviewId = new Map(readableTextHints.map((hint) => [hint.review_id, hint]));
  return (
    <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold text-ink">
            Reading order review
          </p>
          <p className="text-xs text-ink-muted mt-1">
            Reorder content on the page or hide repeated side material from assistive technology, then rerun the checks.
          </p>
        </div>
        {editablePages.length > 0 && (
          <label className="text-xs text-ink-muted">
            <span className="block mb-1">Page</span>
            <select
              value={editorPage ?? ""}
              onChange={(e) => onSelectPage(Number(e.target.value))}
              className="
                rounded-lg border border-ink/10 bg-white px-3 py-2 text-sm text-ink
                focus:outline-none focus:ring-2 focus:ring-accent/20
              "
            >
              {editablePages.map((pageNumber) => (
                <option key={`${taskId}-editor-page-${pageNumber}`} value={pageNumber}>
                  Page {pageNumber}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {editorPage && pageElements.length > 0 ? (
        <div className="mt-4 space-y-2">
          {editorPagePreviewUrl && (
            <a
              href={editorPagePreviewUrl}
              target="_blank"
              rel="noreferrer"
              className="block rounded-lg border border-ink/8 bg-paper-warm/60 p-2 no-underline"
            >
              <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted mb-2">
                Page preview
              </p>
              <img
                src={editorPagePreviewUrl}
                alt={`Preview of page ${editorPage}`}
                loading="lazy"
                className="w-full rounded-md border border-ink/6 bg-paper-warm object-cover"
              />
            </a>
          )}
          <div className="flex flex-wrap items-center gap-2 pb-2">
            <button
              type="button"
              onClick={onUndo}
              disabled={structureHistoryLength === 0 || updateStructurePending}
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
              onClick={onRedo}
              disabled={structureFutureLength === 0 || updateStructurePending}
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
              onClick={() => onResetPage(editorPage)}
              disabled={updateStructurePending}
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
              {(() => {
                const textHint = hintsByReviewId.get(element.review_id);
                return (
                  <>
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
                      onChange={(e) => onUpdateElementType(element.review_id, e.target.value)}
                      disabled={updateStructurePending}
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
                        onChange={(e) => onUpdateHeadingLevel(element.review_id, Number(e.target.value))}
                        disabled={updateStructurePending}
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
                    onClick={() => onMoveElement(editorPage, element.review_id, -1)}
                    disabled={index === 0 || updateStructurePending}
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
                    onClick={() => onMoveElement(editorPage, element.review_id, 1)}
                    disabled={index === pageElements.length - 1 || updateStructurePending}
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
                    onClick={() => onToggleArtifact(element.review_id)}
                    disabled={updateStructurePending}
                    className="
                      px-3 py-2 rounded-lg text-xs font-medium
                      bg-white border border-ink/10 text-ink
                      disabled:opacity-40 disabled:cursor-not-allowed
                    "
                  >
                    {element.type === "artifact" ? "Show as Content" : "Hide from Assistive Tech"}
                  </button>
                </div>
              </div>
              {textHint && (
                <div className="mt-3 rounded-lg border border-accent-light bg-white/80 px-3 py-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                      Gemini readable-text hint
                    </p>
                    <span
                      className={`
                        rounded-full px-2 py-1 text-[11px]
                        ${
                          textHint.should_block_accessibility
                            ? "bg-error-light text-error"
                            : "bg-accent-glow text-accent"
                        }
                      `}
                    >
                      {textHint.should_block_accessibility ? "Needs attention" : "Check this"}
                    </span>
                  </div>
                  <p className="mt-2 text-sm text-ink break-words">
                    {textHint.readable_text_hint}
                  </p>
                  <p className="mt-1 text-xs text-ink-muted">
                    {textHint.confidence ? `${textHint.confidence} confidence` : "Confidence not provided"}
                    {textHint.issue_type ? ` · ${textHint.issue_type.replaceAll("_", " ")}` : ""}
                  </p>
                  {textHint.reason && (
                    <p className="mt-1 text-xs text-ink-muted">{textHint.reason}</p>
                  )}
                </div>
              )}
                  </>
                );
              })()}
            </div>
          ))}
          <div className="flex items-center justify-between gap-3 pt-2">
            <p className="text-xs text-ink-muted">
              {hasUnsavedEdits
                ? "Saving reruns tagging, compliance checks, and fidelity checks on the edited structure."
                : "No unsaved structure edits yet."}
            </p>
            <button
              type="button"
              onClick={onSave}
              disabled={updateStructurePending || !hasUnsavedEdits}
              className="
                px-4 py-2 rounded-lg text-sm font-medium
                bg-accent text-white
                hover:bg-accent/90 transition-colors
                disabled:opacity-50 disabled:cursor-not-allowed
              "
            >
              {updateStructurePending ? "Reprocessing..." : "Save Reading Order Changes"}
            </button>
          </div>
          {updateStructureError && (
            <p className="text-xs text-error">
              {updateStructureError.message || "Failed to save structure edits"}
            </p>
          )}
        </div>
      ) : (
        <p className="mt-3 text-xs text-ink-muted">
          No editable structure elements are available for this page yet.
        </p>
      )}
    </div>
  );
}
