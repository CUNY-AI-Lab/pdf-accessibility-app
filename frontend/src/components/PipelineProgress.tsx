import type { PipelineStep, StepName } from "../types";
import { CheckIcon, XIcon } from "./Icons";

const STEP_META: Record<StepName, { label: string; description: string; icon: string }> = {
  classify: {
    label: "Classify",
    description: "Detecting document type",
    icon: "M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2M9 5h6",
  },
  ocr: {
    label: "OCR",
    description: "Extracting text from images",
    icon: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  },
  structure: {
    label: "Structure",
    description: "Analyzing document layout",
    icon: "M3 3h18v18H3zM3 9h18M3 15h18M9 3v18",
  },
  alt_text: {
    label: "Alt Text",
    description: "Generating image descriptions",
    icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  },
  tagging: {
    label: "Tag",
    description: "Writing accessibility tags",
    icon: "M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82zM7 7h.01",
  },
  validation: {
    label: "Validate",
    description: "Checking PDF/UA compliance",
    icon: "M9 12l2 2 4-4m6 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  },
  fidelity: {
    label: "Fidelity",
    description: "Checking content and reading fidelity",
    icon: "M12 2l3 7 7 3-7 3-3 7-3-7-7-3 7-3 3-7z",
  },
};

interface PipelineProgressProps {
  steps: PipelineStep[];
}

export default function PipelineProgress({ steps }: PipelineProgressProps) {
  return (
    <div className="space-y-1">
      {steps.map((step, i) => {
        const meta = STEP_META[step.step_name];
        const isLast = i === steps.length - 1;

        return (
          <div key={step.step_name} className="flex items-start gap-4">
            {/* Vertical connector + icon */}
            <div className="flex flex-col items-center">
              <div
                className={`
                  w-10 h-10 rounded-xl flex items-center justify-center
                  transition-all duration-300
                  ${
                    step.status === "complete"
                      ? "bg-success text-white shadow-sm"
                      : step.status === "running"
                        ? "bg-accent-bright text-white shadow-md animate-pulse-soft"
                        : step.status === "failed"
                          ? "bg-error text-white shadow-sm"
                          : step.status === "skipped"
                            ? "bg-paper-warm text-ink-muted"
                            : "bg-paper-warm text-ink-muted/50"
                  }
                `}
              >
                {step.status === "complete" ? (
                  <CheckIcon size={16} />
                ) : step.status === "failed" ? (
                  <XIcon size={16} />
                ) : step.status === "skipped" ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M5 4h4l5 8-5 8H5l5-8z" />
                    <path d="M13 4h4l5 8-5 8h-4l5-8z" />
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d={meta.icon} />
                  </svg>
                )}
              </div>
              {!isLast && (
                <div
                  className={`
                    w-0.5 h-6 rounded-full mt-1
                    ${step.status === "complete" ? "bg-success/30" : "bg-ink/8"}
                  `}
                />
              )}
            </div>

            {/* Content */}
            <div className="pt-2 pb-4 min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h4
                  className={`
                    text-sm font-semibold
                    ${
                      step.status === "running"
                        ? "text-accent"
                        : step.status === "complete"
                          ? "text-ink"
                          : step.status === "failed"
                            ? "text-error"
                            : "text-ink-muted"
                    }
                  `}
                >
                  {meta.label}
                </h4>
                {step.status === "running" && (
                  <div className="flex gap-0.5">
                    <span className="w-1 h-1 rounded-full bg-accent-bright animate-pulse-soft" style={{ animationDelay: "0ms" }} />
                    <span className="w-1 h-1 rounded-full bg-accent-bright animate-pulse-soft" style={{ animationDelay: "200ms" }} />
                    <span className="w-1 h-1 rounded-full bg-accent-bright animate-pulse-soft" style={{ animationDelay: "400ms" }} />
                  </div>
                )}
                {step.status === "skipped" && (
                  <span className="text-xs text-ink-muted bg-paper-warm px-2 py-0.5 rounded-full">
                    Skipped
                  </span>
                )}
              </div>
              <p className="text-xs text-ink-muted mt-0.5">
                {step.error || meta.description}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
