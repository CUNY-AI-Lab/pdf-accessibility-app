import { useState } from "react";

import type { AltText } from "../types";
import PreviewImage from "./PreviewImage";

interface AltTextRecommendationCardProps {
  altText: AltText;
  accepting?: boolean;
  acceptError?: Error | null;
  suggesting?: boolean;
  suggestError?: Error | null;
  onAccept: (figureIndex: number) => Promise<void> | void;
  onSuggestAlternative: (figureIndex: number, feedback?: string) => Promise<void> | void;
}

function recommendationForAltText(altText: AltText): {
  kind: "decorative" | "alt_text" | "missing";
  text: string;
} {
  const raw = String(altText.edited_text || altText.generated_text || "").trim();
  if (!raw) {
    return { kind: "missing", text: "" };
  }
  if (raw.toLowerCase() == "decorative") {
    return { kind: "decorative", text: "The model recommends hiding this figure from assistive technology because it appears decorative or redundant." };
  }
  return { kind: "alt_text", text: raw };
}

export default function AltTextRecommendationCard({
  altText,
  accepting = false,
  acceptError = null,
  suggesting = false,
  suggestError = null,
  onAccept,
  onSuggestAlternative,
}: AltTextRecommendationCardProps) {
  const [feedback, setFeedback] = useState("");
  const recommendation = recommendationForAltText(altText);
  const trimmedFeedback = feedback.trim();

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">Figure {altText.figure_index + 1}</h3>
          <p className="mt-1 text-sm text-ink-light">
            Review the recommendation for this figure and accept it or describe what should change.
          </p>
        </div>
        <span className="rounded-full bg-error-light px-2 py-1 text-[11px] font-medium text-error">
          Must Fix
        </span>
      </div>

      <div className="mt-4">
        <PreviewImage
          src={altText.image_url}
          href={altText.image_url}
          alt={`Figure ${altText.figure_index + 1}`}
          title={`Figure ${altText.figure_index + 1} preview`}
          imageClassName="w-full rounded-md border border-ink/6 bg-paper-warm object-contain max-h-72"
        />
      </div>

      <div className="mt-4 rounded-lg border border-accent-light bg-accent-glow/60 px-3 py-3">
        <p className="text-xs font-semibold text-ink">Recommendation</p>
        {recommendation.kind === "missing" ? (
          <p className="mt-3 text-sm text-ink-muted">
            No recommendation is available yet. Describe what the figure should convey and the model will try again.
          </p>
        ) : (
          <p className="mt-3 text-sm text-ink leading-relaxed">{recommendation.text}</p>
        )}

        <div className="mt-4 rounded-lg border border-accent-light bg-white/70 px-3 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => onAccept(altText.figure_index)}
              disabled={accepting || recommendation.kind === "missing"}
              className="
                px-3 py-2 rounded-lg text-xs font-medium
                bg-accent text-white
                hover:bg-accent/90 transition-colors
                disabled:opacity-50 disabled:cursor-not-allowed
              "
            >
              {accepting ? "Applying..." : "Accept Recommendation"}
            </button>
            <span className="text-xs text-ink-muted">
              If this is not right, describe what should change below.
            </span>
          </div>
          {acceptError && (
            <p className="mt-2 text-xs text-error">
              {acceptError.message || "Failed to apply the recommendation"}
            </p>
          )}
        </div>

        <div className="mt-3 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          <p className="text-xs font-semibold text-ink">Suggest alternative</p>
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={3}
            placeholder="Example: This should be decorative. This figure needs the caption summarized. This screenshot should mention the search box and button."
            className="
              mt-3 w-full rounded-lg border border-ink/10 bg-paper-warm/40 px-3 py-2
              text-sm text-ink placeholder:text-ink-muted/70
              focus:outline-none focus:ring-2 focus:ring-accent/20
            "
          />
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => onSuggestAlternative(altText.figure_index, trimmedFeedback)}
              disabled={trimmedFeedback.length === 0 || suggesting}
              className="
                px-3 py-2 rounded-lg text-xs font-medium
                bg-accent text-white
                hover:bg-accent/90 transition-colors
                disabled:opacity-50 disabled:cursor-not-allowed
              "
            >
              {suggesting ? "Revising..." : "Revise Recommendation"}
            </button>
            {suggestError && (
              <p className="text-xs text-error">
                {suggestError.message || "Failed to revise the recommendation"}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
