import { useCallback, useState } from "react";
import type { AltText, AltTextStatus } from "../types";
import { CheckIcon } from "./Icons";

interface AltTextEditorProps {
  altText: AltText;
  onUpdate: (figureIndex: number, editedText?: string, status?: AltTextStatus) => void;
  saving?: boolean;
}

export default function AltTextEditor({
  altText,
  onUpdate,
  saving,
}: AltTextEditorProps) {
  const [text, setText] = useState(
    altText.edited_text || altText.generated_text || "",
  );
  const [isEditing, setIsEditing] = useState(false);

  const handleApprove = useCallback(() => {
    onUpdate(altText.figure_index, text, "approved");
    setIsEditing(false);
  }, [altText.figure_index, text, onUpdate]);

  const handleReject = useCallback(() => {
    onUpdate(altText.figure_index, undefined, "rejected");
    setIsEditing(false);
  }, [altText.figure_index, onUpdate]);

  const isApproved = altText.status === "approved";
  const isRejected = altText.status === "rejected";

  return (
    <div
      className={`
        rounded-xl border overflow-hidden transition-all duration-200
        ${
          isApproved
            ? "border-success/30 bg-success-light/30"
            : isRejected
              ? "border-error/20 bg-error-light/30"
              : "border-ink/8 bg-cream"
        }
      `}
    >
      <div className="flex flex-col md:flex-row">
        {/* Figure image */}
        <div className="md:w-64 shrink-0 bg-paper-warm/50 p-4 flex items-center justify-center border-b md:border-b-0 md:border-r border-ink/6">
          <img
            src={altText.image_url}
            alt={text || "Figure requiring alt text"}
            className="max-w-full max-h-48 object-contain rounded-lg"
          />
        </div>

        {/* Alt text content */}
        <div className="flex-1 p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-ink-muted bg-paper-warm px-2 py-0.5 rounded">
                Figure {altText.figure_index + 1}
              </span>
              {isApproved && (
                <span className="text-xs font-medium text-success bg-success-light px-2 py-0.5 rounded-full flex items-center gap-1">
                  <CheckIcon size={10} />
                  Approved
                </span>
              )}
              {isRejected && (
                <span className="text-xs font-medium text-error bg-error-light px-2 py-0.5 rounded-full">
                  Decorative
                </span>
              )}
            </div>
            {!isEditing && !isApproved && !isRejected && (
              <button
                type="button"
                onClick={() => setIsEditing(true)}
                className="text-xs text-accent hover:text-accent-bright font-medium transition-colors"
              >
                Edit
              </button>
            )}
            {(isApproved || isRejected) && (
              <button
                type="button"
                onClick={() => {
                  setIsEditing(true);
                  onUpdate(altText.figure_index, text, "pending_review");
                }}
                className="text-xs text-ink-muted hover:text-ink font-medium transition-colors"
              >
                Undo
              </button>
            )}
          </div>

          {isEditing ? (
            <>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={3}
                className="
                  w-full px-3 py-2.5 rounded-lg border border-ink/12
                  bg-white text-sm text-ink font-body
                  focus:outline-none focus:border-accent-bright focus:ring-2 focus:ring-accent-bright/20
                  resize-y transition-all
                "
                placeholder="Describe this figure for screen reader users..."
              />
              <div className="flex items-center gap-2 mt-3">
                <button
                  type="button"
                  onClick={handleApprove}
                  disabled={saving || !text.trim()}
                  className="
                    px-4 py-2 rounded-lg text-sm font-medium
                    bg-success text-white
                    hover:bg-success/90 transition-colors
                    disabled:opacity-50 disabled:cursor-not-allowed
                  "
                >
                  {saving ? "Saving..." : "Approve"}
                </button>
                <button
                  type="button"
                  onClick={handleReject}
                  disabled={saving}
                  className="
                    px-4 py-2 rounded-lg text-sm font-medium
                    bg-paper-warm text-ink-muted
                    hover:bg-error-light hover:text-error transition-colors
                    disabled:opacity-50
                  "
                >
                  Mark Decorative
                </button>
                <button
                  type="button"
                  onClick={() => setIsEditing(false)}
                  className="px-3 py-2 text-sm text-ink-muted hover:text-ink transition-colors"
                >
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <div>
              <p className="text-sm text-ink leading-relaxed">
                {text || (
                  <span className="italic text-ink-muted">
                    No alt text generated yet.
                  </span>
                )}
              </p>
              {!isApproved && !isRejected && (
                <div className="flex items-center gap-2 mt-3">
                  <button
                    type="button"
                    onClick={handleApprove}
                    disabled={saving || !text.trim()}
                    className="
                      px-4 py-2 rounded-lg text-sm font-medium
                      bg-success text-white
                      hover:bg-success/90 transition-colors
                      disabled:opacity-50 disabled:cursor-not-allowed
                    "
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    onClick={() => setIsEditing(true)}
                    className="
                      px-4 py-2 rounded-lg text-sm font-medium
                      bg-paper-warm text-ink-muted
                      hover:bg-accent-light hover:text-accent transition-colors
                    "
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={handleReject}
                    disabled={saving}
                    className="
                      px-3 py-2 text-sm text-ink-muted hover:text-error transition-colors
                    "
                  >
                    Decorative
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
