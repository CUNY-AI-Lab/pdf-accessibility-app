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
import { jobPollInterval, jobRevision, loadReviewSnapshot, outputReady, projectReviewContext } from "../state/jobView";
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
      return jobPollInterval(job);
    },
  });
}


export function useResolveReviewTask(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId }: { taskId: number }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/review-tasks/${taskId}/resolve`,
        { method: "POST" },
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
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

export function useEditAppliedChange(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ changeId, text }: { changeId: number; text: string }) =>
      apiFetch<{ status: string; message: string; job_status: string }>(
        `/jobs/${jobId}/applied-changes/${changeId}/edit`,
        {
          method: "POST",
          body: JSON.stringify({ text }),
        },
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
}


// ── Mutations ──

export function useCreateJobs() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => apiFetch<{ jobs: Job[] }>(
      `/jobs?filename=${encodeURIComponent(file.name)}`,
      { method: "POST", headers: { "Content-Type": "application/pdf" }, body: file },
    ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
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

/** One cache entry for the entire review context, scoped to observed output. */
export function useReviewContext(job?: Job) {
  const query = useQuery({
    queryKey: ["jobs", job?.id, "review-context", job ? jobRevision(job) : null],
    enabled: outputReady(job),
    queryFn: () => {
      if (!job) throw new Error("Review needs a job");
      const read = <T,>(path: string) => apiFetch<T>(`/jobs/${job.id}${path}`, { cache: "no-store" });
      return loadReviewSnapshot(job, () => read<Job>(""), async () => {
        const [tasks, changes, figures, validation] = await Promise.all([
          read<ReviewTask[]>("/review-tasks"), read<AppliedChange[]>("/applied-changes"),
          read<AppliedChange[]>("/figure-changes"), read<ValidationReport>("/download/report"),
        ]);
        return { tasks, changes, figures, validation };
      });
    },
    refetchInterval: 30000,
  });
  return projectReviewContext(job, query);
}
