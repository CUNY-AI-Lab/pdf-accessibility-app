import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PipelineStep, ProgressEvent, StepName } from "../types";

const INITIAL_STEPS: PipelineStep[] = [
  { step_name: "classify", status: "pending" },
  { step_name: "ocr", status: "pending" },
  { step_name: "structure", status: "pending" },
  { step_name: "alt_text", status: "pending" },
  { step_name: "tagging", status: "pending" },
  { step_name: "validation", status: "pending" },
  { step_name: "fidelity", status: "pending" },
];

function createInitialSteps(): PipelineStep[] {
  return INITIAL_STEPS.map((step) => ({ ...step }));
}

const MAX_RETRIES = 8;
const MAX_BACKOFF_MS = 30_000;

export function useJobProgress(jobId: string, active = true) {
  const [stepsByJob, setStepsByJob] = useState<Record<string, PipelineStep[]>>(
    {},
  );
  const [connected, setConnected] = useState(false);
  const queryClient = useQueryClient();
  const sourceRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const steps = useMemo(
    () => stepsByJob[jobId] ?? createInitialSteps(),
    [jobId, stepsByJob],
  );

  const updateStep = useCallback((event: ProgressEvent) => {
    if (event.step === "review" || event.step === "error") return;

    setStepsByJob((prev) => {
      const currentSteps = prev[jobId] ?? createInitialSteps();
      const nextSteps = currentSteps.map((s) =>
        s.step_name === (event.step as StepName)
          ? {
              ...s,
              status: event.status as PipelineStep["status"],
              started_at: event.timestamp,
              completed_at:
                event.status === "complete" || event.status === "failed" || event.status === "skipped"
                  ? event.timestamp
                  : undefined,
              error: event.status === "failed" ? event.message : undefined,
              result: event.result,
            }
          : s,
      );

      return {
        ...prev,
        [jobId]: nextSteps,
      };
    });
  }, [jobId]);

  const connect = useCallback(() => {
    // Clean up any existing connection
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }

    const source = new EventSource(`/api/jobs/${jobId}/progress`);
    sourceRef.current = source;

    source.onopen = () => {
      retryCountRef.current = 0;
      setConnected(true);
    };

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

      if (retryCountRef.current < MAX_RETRIES) {
        const backoffMs = Math.min(
          1000 * 2 ** retryCountRef.current,
          MAX_BACKOFF_MS,
        );
        retryCountRef.current += 1;
        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          connect();
        }, backoffMs);
      }
    };
  }, [jobId, updateStep, queryClient]);

  useEffect(() => {
    if (!active) return;

    connect();

    return () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      setConnected(false);
    };
  }, [active, connect]);

  return { steps, connected };
}
