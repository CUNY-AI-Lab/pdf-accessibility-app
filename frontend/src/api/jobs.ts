import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type {
  AltText,
  AltTextRecommendationApplyResult,
  Job,
  ReviewTask,
  ValidationReport,
} from "../types";
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

export function useSuggestReviewTask(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId, feedback }: { taskId: number; feedback?: string }) =>
      apiFetch<ReviewTask>(`/jobs/${jobId}/review-tasks/${taskId}/suggest`, {
        method: "POST",
        body: JSON.stringify({
          feedback,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
    },
  });
}

export function useApplyReviewRecommendation(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId }: { taskId: number }) =>
      apiFetch<{ status: string; message: string }>(
        `/jobs/${jobId}/review-tasks/${taskId}/apply-recommendation`,
        {
          method: "POST",
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "validation"] });
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

export function useAcceptAltTextRecommendation(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ figureIndex }: { figureIndex: number }) =>
      apiFetch<AltTextRecommendationApplyResult>(
        `/jobs/${jobId}/alt-texts/${figureIndex}/accept-recommendation`,
        {
          method: "POST",
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "alt-texts"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "validation"] });
    },
  });
}

export function useSuggestAltText(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      figureIndex,
      feedback,
    }: {
      figureIndex: number;
      feedback?: string;
    }) =>
      apiFetch<AltText>(`/jobs/${jobId}/alt-texts/${figureIndex}/suggest`, {
        method: "POST",
        body: JSON.stringify({ feedback }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "alt-texts"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
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
