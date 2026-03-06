import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { AltText, Job, ReviewTask, ValidationReport } from "../types";
import { apiFetch } from "./client";

// ── Queries ──

export function useJobs(status?: string) {
  return useQuery({
    queryKey: ["jobs", status],
    queryFn: () =>
      apiFetch<{ jobs: Job[]; total: number }>(
        `/jobs${status ? `?status=${status}` : ""}`,
      ),
    refetchInterval: 5000,
  });
}

export function useJob(id: string) {
  return useQuery({
    queryKey: ["jobs", id],
    queryFn: () => apiFetch<Job>(`/jobs/${id}`),
    refetchInterval: 3000,
  });
}

export function useStructure(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "structure"],
    queryFn: () => apiFetch<Record<string, unknown>>(`/jobs/${jobId}/structure`),
    enabled,
  });
}

export function useAltTexts(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "alt-texts"],
    queryFn: () => apiFetch<AltText[]>(`/jobs/${jobId}/alt-texts`),
    enabled,
  });
}

export function useReviewTasks(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "review-tasks"],
    queryFn: () => apiFetch<ReviewTask[]>(`/jobs/${jobId}/review-tasks`),
    enabled,
  });
}

export function useUpdateReviewTask(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      taskId,
      status,
      resolutionNote,
      evidence,
    }: {
      taskId: number;
      status?: "pending_review" | "resolved";
      resolutionNote?: string;
      evidence?: Record<string, string>;
    }) =>
      apiFetch<ReviewTask>(`/jobs/${jobId}/review-tasks/${taskId}`, {
        method: "PUT",
        body: JSON.stringify({
          status,
          resolution_note: resolutionNote,
          evidence,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
    },
  });
}

export function useValidation(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "validation"],
    queryFn: () =>
      apiFetch<ValidationReport>(`/jobs/${jobId}/download/report`),
    enabled,
  });
}

// ── Mutations ──

export function useCreateJobs() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (files: File[]) => {
      const formData = new FormData();
      files.forEach((file) => formData.append("files", file));
      return apiFetch<{ jobs: Job[] }>("/jobs", {
        method: "POST",
        body: formData,
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useUpdateAltText(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      figureIndex,
      editedText,
      status,
    }: {
      figureIndex: number;
      editedText?: string;
      status?: string;
    }) =>
      apiFetch<AltText>(`/jobs/${jobId}/alt-texts/${figureIndex}`, {
        method: "PUT",
        body: JSON.stringify({
          edited_text: editedText,
          status,
        }),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["jobs", jobId, "alt-texts"],
      }),
  });
}

export function useApproveReview(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ status: string }>(`/jobs/${jobId}/approve`, {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "validation"] });
    },
  });
}

export function useDeleteJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      apiFetch<void>(`/jobs/${jobId}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
}
