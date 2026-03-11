import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type {
  AppliedChange,
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

export function useReviewTasks(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "review-tasks"],
    queryFn: () => apiFetch<ReviewTask[]>(`/jobs/${jobId}/review-tasks`),
    enabled,
  });
}

export function useAppliedChanges(jobId: string, enabled = true) {
  return useQuery({
    queryKey: ["jobs", jobId, "applied-changes"],
    queryFn: () => apiFetch<AppliedChange[]>(`/jobs/${jobId}/applied-changes`),
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
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "applied-changes"] });
    },
  });
}

export function useKeepAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId }: { changeId: number }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/keep`,
        { method: "POST" },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "applied-changes"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "validation"] });
    },
  });
}

export function useUndoAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId }: { changeId: number }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/undo`,
        { method: "POST" },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "applied-changes"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "review-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "validation"] });
    },
  });
}

export function useSuggestAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId, feedback }: { changeId: number; feedback?: string }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/suggest`,
        {
          method: "POST",
          body: JSON.stringify({ feedback }),
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
      queryClient.invalidateQueries({ queryKey: ["jobs", jobId, "applied-changes"] });
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

export function useDeleteJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      apiFetch<void>(`/jobs/${jobId}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
}
