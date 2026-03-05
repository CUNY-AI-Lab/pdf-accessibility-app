import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PipelineStep, ProgressEvent, StepName } from "../types";

const INITIAL_STEPS: PipelineStep[] = [
  { step_name: "classify", status: "pending" },
  { step_name: "ocr", status: "pending" },
  { step_name: "structure", status: "pending" },
  { step_name: "alt_text", status: "pending" },
  { step_name: "tagging", status: "pending" },
  { step_name: "validation", status: "pending" },
];

export function useJobProgress(jobId: string, active = true) {
  const [steps, setSteps] = useState<PipelineStep[]>(INITIAL_STEPS);
  const [connected, setConnected] = useState(false);
  const queryClient = useQueryClient();
  const sourceRef = useRef<EventSource | null>(null);

  // Reset steps when jobId changes
  useEffect(() => {
    setSteps(INITIAL_STEPS);
  }, [jobId]);

  const updateStep = useCallback((event: ProgressEvent) => {
    if (event.step === "review" || event.step === "error") return;

    setSteps((prev) =>
      prev.map((s) =>
        s.step_name === (event.step as StepName)
          ? {
              ...s,
              status: event.status as PipelineStep["status"],
              started_at: event.timestamp,
              completed_at:
                event.status === "complete" || event.status === "failed"
                  ? event.timestamp
                  : undefined,
              error: event.status === "failed" ? event.message : undefined,
            }
          : s,
      ),
    );
  }, []);

  useEffect(() => {
    if (!active) return;

    const source = new EventSource(`/api/jobs/${jobId}/progress`);
    sourceRef.current = source;

    source.onopen = () => setConnected(true);

    source.addEventListener("progress", (e) => {
      try {
        const data: ProgressEvent = JSON.parse(e.data);
        updateStep(data);

        if (data.status === "complete" || data.status === "failed") {
          queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
        }
        if (data.step === "review") {
          queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
        }
      } catch {
        // ignore parse errors
      }
    });

    source.onerror = () => {
      setConnected(false);
      source.close();
      sourceRef.current = null;
    };

    return () => {
      source.close();
      sourceRef.current = null;
      setConnected(false);
    };
  }, [jobId, active, updateStep, queryClient]);

  return { steps, connected };
}
