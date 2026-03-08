import type { ReviewTask } from "../types";
import type { FontReviewTarget, LlmSuggestion } from "../pages/reviewHelpers";
import { pluralize } from "../utils/format";
import {
  actualTextCandidateForTarget,
  actualTextKeyForTarget,
  fontTargetPreviewUrl,
  manualActualTextAttempts,
  manualFontMappingAttempts,
} from "../pages/reviewHelpers";
import type { FontMutationState } from "./ReviewTaskCard";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface FontTargetPanelProps {
  jobId: string;
  task: ReviewTask;
  reviewTargets: FontReviewTarget[];
  llmSuggestion: LlmSuggestion | null;
  actualTextDrafts: Record<string, string>;
  fontMutation: FontMutationState;
  onActualTextDraftChange: (key: string, value: string) => void;
  onApplyActualText: (task: ReviewTask, target: FontReviewTarget) => void;
  onApplyFontMap: (task: ReviewTask, target: FontReviewTarget) => void;
  onUseSuggestedActualText: (task: ReviewTask, target: FontReviewTarget, proposedText: string) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function FontTargetPanel({
  jobId,
  task,
  reviewTargets,
  llmSuggestion,
  actualTextDrafts,
  fontMutation,
  onActualTextDraftChange,
  onApplyActualText,
  onApplyFontMap,
  onUseSuggestedActualText,
}: FontTargetPanelProps) {
  const {
    applyingActualTextKey,
    applyingFontMapKey,
    actualText: { isPending: applyFontActualTextPending, error: applyFontActualTextError },
    unicodeMapping: { isPending: applyFontUnicodeMappingPending, error: applyFontUnicodeMappingError },
  } = fontMutation;
  const actualTextAttempts = manualActualTextAttempts(task);
  const fontMappingAttempts = manualFontMappingAttempts(task);

  return (
    <>
      {/* Targeted findings */}
      {reviewTargets.length > 0 && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-paper-warm/70 px-3 py-3">
          <p className="text-xs font-semibold text-ink mb-2">Flagged text locations</p>
          <div className="space-y-2">
            {reviewTargets.map((target, index) => (
              <div
                key={`${task.id}-target-${index}`}
                className="rounded-lg bg-white/70 px-3 py-2"
              >
                {(() => {
                  const actualtextCandidate = actualTextCandidateForTarget(llmSuggestion, target);
                  const targetPreview = fontTargetPreviewUrl(jobId, task.id, target);
                  return (
                    <>
                      <p className="text-sm text-ink">
                        {target.page ? `Page ${target.page}` : "Page unknown"}
                        {target.font ? ` · ${target.font}` : ""}
                        {target.rule_id ? ` · ${target.rule_id}` : ""}
                        {typeof target.operator_index === "number" ? ` · operator ${target.operator_index}` : ""}
                        {typeof target.count === "number" ? ` · ${target.count} ${pluralize(target.count, "occurrence")}` : ""}
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
                            Gemini spoken-text suggestion
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
                              onClick={() => onUseSuggestedActualText(task, target, actualtextCandidate.proposed_actualtext ?? "")}
                              className="
                                px-3 py-2 rounded-lg text-xs font-medium
                                bg-white text-ink border border-ink/10
                                hover:border-accent-light transition-colors
                              "
                            >
                              Use this text
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
                                  onActualTextDraftChange(
                                    actualTextKeyForTarget(task.id, target),
                                    e.target.value,
                                  )
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
                                onClick={() => onApplyActualText(task, target)}
                                disabled={
                                  applyingActualTextKey === actualTextKeyForTarget(task.id, target)
                                  || applyFontActualTextPending
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
                                  : "Set Spoken Text"}
                              </button>
                              <button
                                type="button"
                                onClick={() => onApplyFontMap(task, target)}
                                disabled={
                                  applyingFontMapKey === actualTextKeyForTarget(task.id, target)
                                  || applyFontUnicodeMappingPending
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
                                  : "Fix Matching Symbols"}
                              </button>
                            </div>
                            <p className="mt-2 text-[11px] text-ink-muted">
                              “Set Spoken Text” changes only this one location for screen readers and copy/paste. “Fix Matching Symbols” updates the font mapping for every matching use of this symbol in the document.
                            </p>
                          </>
                        )}
                    </>
                  );
                })()}
              </div>
            ))}
          </div>
          {applyFontActualTextError && (
            <p className="mt-3 text-xs text-error">
              {applyFontActualTextError.message || "Failed to apply ActualText remediation"}
            </p>
          )}
          {applyFontUnicodeMappingError && (
            <p className="mt-3 text-xs text-error">
              {applyFontUnicodeMappingError.message || "Failed to apply font-map remediation"}
            </p>
          )}
        </div>
      )}

      {/* Applied ActualText attempts */}
      {actualTextAttempts.length > 0 && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <p className="text-xs font-semibold text-ink mb-2">
            Applied spoken-text fixes
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

      {/* Applied font-map attempts */}
      {fontMappingAttempts.length > 0 && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <p className="text-xs font-semibold text-ink mb-2">
            Applied symbol-mapping fixes
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
    </>
  );
}
