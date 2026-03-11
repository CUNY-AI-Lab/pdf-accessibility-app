import { useState } from "react";
import type { AppliedChange } from "../types";

interface AppliedChangeCardProps {
  change: AppliedChange;
  onKeep: (change: AppliedChange) => Promise<void> | void;
  onUndo: (change: AppliedChange) => Promise<void> | void;
  onRevise: (change: AppliedChange, feedback?: string) => Promise<void> | void;
  keeping: boolean;
  undoing: boolean;
  revising: boolean;
  actionError?: Error | null;
}

export default function AppliedChangeCard({
  change,
  onKeep,
  onUndo,
  onRevise,
  keeping,
  undoing,
  revising,
  actionError,
}: AppliedChangeCardProps) {
  const [feedback, setFeedback] = useState("");
  const severityClasses = {
    high: "bg-error-light text-error",
    medium: "bg-warning-light text-warning",
    low: "bg-info-light text-info",
  } as const;

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{change.title}</h3>
          <p className="mt-1 text-sm text-ink-light">{change.detail}</p>
        </div>
        <span className={`rounded-full px-2 py-1 text-[11px] font-medium ${severityClasses[change.importance]}`}>
          Review
        </span>
      </div>

      {typeof change.metadata.summary === "string" && change.metadata.summary.trim() && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <p className="text-sm text-ink">{change.metadata.summary}</p>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onKeep(change)}
          disabled={keeping || undoing || revising}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {keeping ? "Keeping..." : "Keep"}
        </button>
        <button
          type="button"
          onClick={() => onUndo(change)}
          disabled={keeping || undoing || revising}
          className="rounded-lg border border-ink/10 bg-white px-4 py-2 text-sm font-medium text-ink disabled:opacity-50"
        >
          {undoing ? "Undoing..." : "Undo"}
        </button>
      </div>

      <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 p-3">
        <label className="block text-xs font-medium uppercase tracking-wide text-ink-muted mb-2">
          Revise
        </label>
        <textarea
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          rows={3}
          className="w-full rounded-lg border border-ink/10 bg-white px-3 py-2 text-sm text-ink placeholder:text-ink-muted/70"
          placeholder="Explain what should change and the app will retry this edit."
        />
        <div className="mt-3 flex justify-end">
          <button
            type="button"
            onClick={() => onRevise(change, feedback)}
            disabled={!feedback.trim() || keeping || undoing || revising}
            className="rounded-lg border border-accent/20 bg-accent/10 px-4 py-2 text-sm font-medium text-accent disabled:opacity-50"
          >
            {revising ? "Revising..." : "Revise"}
          </button>
        </div>
      </div>

      {actionError && (
        <div className="mt-3 rounded-lg border border-error/25 bg-error-light/40 px-3 py-2 text-sm text-error">
          {actionError.message || "Action failed"}
        </div>
      )}
    </div>
  );
}
