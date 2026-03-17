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
    refetchInterval: (query) => {
      const data = query.state.data as { jobs: Job[]; total: number } | undefined;
      return data?.jobs.some((job) => job.status === "queued" || job.status === "processing")
        ? 5000
        : 30000;
    },
  });
}

export function useJob(id: string) {
  return useQuery({
    queryKey: ["jobs", id],
    queryFn: () => apiFetch<Job>(`/jobs/${id}`),
    refetchInterval: (query) => {
      const job = query.state.data as Job | undefined;
      if (!job) return false;
      return job.status === "queued" || job.status === "processing" ? 3000 : false;
    },
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

export function useKeepAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId }: { changeId: number }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/keep`,
        { method: "POST" },
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
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
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

export function useReviseAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId, feedback }: { changeId: number; feedback?: string }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/revise`,
        {
          method: "POST",
          body: JSON.stringify({ feedback }),
        },
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
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
