import { useState } from "react";
import { apiUrl } from "../api/client";
import type { AppliedChange } from "../types";

interface AppliedChangeCardProps {
  jobId: string;
  change: AppliedChange;
  onKeep: (change: AppliedChange) => Promise<void> | void;
  onUndo: (change: AppliedChange) => Promise<void> | void;
  onRevise: (change: AppliedChange, feedback?: string) => Promise<void> | void;
  onEdit: (change: AppliedChange, text: string) => Promise<void> | void;
  keeping: boolean;
  undoing: boolean;
  revising: boolean;
  editing: boolean;
  actionError?: Error | null;
}

function getAltText(change: AppliedChange): string | null {
  const after = change.after ?? {};
  return (after.edited_text as string) || (after.generated_text as string) || null;
}

function isDecorative(change: AppliedChange): boolean {
  const after = change.after ?? {};
  return after.status === "rejected" || (after.edited_text as string) === "decorative";
}

export default function AppliedChangeCard({
  jobId,
  change,
  onKeep,
  onUndo,
  onRevise,
  onEdit,
  keeping,
  undoing,
  revising,
  editing,
  actionError,
}: AppliedChangeCardProps) {
  const [feedback, setFeedback] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const isFigureDecision = change.change_type === "figure_semantics";
  const figureIndex = typeof change.metadata.figure_index === "number" ? change.metadata.figure_index : null;
  const altText = getAltText(change);
  const decorative = isDecorative(change);
  const anyBusy = keeping || undoing || revising || editing;

  const severityClasses = {
    high: "bg-error-light text-error",
    medium: "bg-warning-light text-warning",
    low: "bg-info-light text-info",
  } as const;

  const keepLabel = keeping ? "Keeping..." : "Keep";
  const undoLabel = undoing ? "Undoing..." : "Undo";
  const retryLabel = revising ? "Retrying..." : "Retry";
  const editSaveLabel = editing ? "Saving..." : "Save";

  const handleStartEdit = () => {
    setEditText(altText ?? "");
    setIsEditing(true);
  };

  const handleCancelEdit = () => {
    setIsEditing(false);
    setEditText("");
  };

  const handleSaveEdit = async () => {
    if (!editText.trim()) return;
    try {
      await onEdit(change, editText.trim());
      // Only close on success — if it fails the user keeps their text
      setIsEditing(false);
    } catch {
      // Error is surfaced via actionError prop; keep form open
    }
  };

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{change.title}</h3>
          <p className="mt-1 text-sm text-ink-light">{change.detail}</p>
        </div>
        <span className={`rounded-full px-2 py-1 text-[11px] font-medium ${severityClasses[change.importance]}`}>
          {isFigureDecision ? "Image QA" : "Review"}
        </span>
      </div>

      {/* Figure image thumbnail */}
      {isFigureDecision && figureIndex !== null && (
        <div className="mt-4">
          <img
            src={apiUrl(`/jobs/${jobId}/figures/${figureIndex}/image`)}
            alt={`Figure ${figureIndex + 1}`}
            className="max-h-48 rounded-lg border border-ink/8 bg-white object-contain"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        </div>
      )}

      {/* Current alt text */}
      {isFigureDecision && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted mb-1.5">
            {decorative ? "Marked decorative" : "Alt text"}
          </label>
          {isEditing ? (
            <div>
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                rows={4}
                className="w-full rounded-lg border border-accent/30 bg-white px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
                autoFocus
              />
              <div className="mt-2 flex gap-2 justify-end">
                <button
                  type="button"
                  onClick={handleCancelEdit}
                  disabled={editing}
                  className="rounded-lg border border-ink/10 bg-white px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleSaveEdit}
                  disabled={!editText.trim() || editing}
                  className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {editSaveLabel}
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <p className="text-sm text-ink flex-1">
                {decorative
                  ? "This image is decorative and will not receive alt text."
                  : altText || <span className="italic text-ink-muted">No description generated</span>}
              </p>
              {!decorative && (
                <button
                  type="button"
                  onClick={handleStartEdit}
                  disabled={anyBusy}
                  className="shrink-0 rounded-lg border border-ink/10 bg-white px-2.5 py-1 text-xs font-medium text-ink-muted hover:text-ink disabled:opacity-50"
                >
                  {altText ? "Edit" : "Add"}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Model reasoning */}
      {typeof change.metadata.summary === "string" && change.metadata.summary.trim() && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-xs font-medium text-ink-muted hover:text-ink">
            Model reasoning
          </summary>
          <p className="mt-1.5 text-sm text-ink-light pl-1">{change.metadata.summary as string}</p>
        </details>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onKeep(change)}
          disabled={anyBusy}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {keepLabel}
        </button>
        <button
          type="button"
          onClick={() => onUndo(change)}
          disabled={anyBusy}
          className="rounded-lg border border-ink/10 bg-white px-4 py-2 text-sm font-medium text-ink disabled:opacity-50"
        >
          {undoLabel}
        </button>
      </div>

      <details className="mt-4">
        <summary className="cursor-pointer text-xs font-medium text-ink-muted hover:text-ink">
          Retry with AI feedback
        </summary>
        <div className="mt-2 rounded-lg border border-ink/8 bg-white/70 p-3">
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={3}
            className="w-full rounded-lg border border-ink/10 bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-muted/70"
            placeholder={
              isFigureDecision
                ? "Describe how this image should be handled. The app will regenerate the description and rerun tagging and validation."
                : "Explain what should change and the app will retry this edit."
            }
          />
          <div className="mt-3 flex justify-end">
            <button
              type="button"
              onClick={() => onRevise(change, feedback)}
              disabled={!feedback.trim() || anyBusy}
              className="rounded-lg border border-accent/20 bg-accent/10 px-4 py-2 text-sm font-medium text-accent disabled:opacity-50"
            >
              {retryLabel}
            </button>
          </div>
        </div>
      </details>

      {actionError && (
        <div className="mt-3 rounded-lg border border-error/25 bg-error-light/40 px-3 py-2 text-sm text-error">
          {actionError.message || "Action failed"}
        </div>
      )}
    </div>
  );
}
