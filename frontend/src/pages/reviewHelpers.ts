import { apiUrl } from "../api/client";
import type { ReviewTask } from "../types";

function metadataPageList(task: ReviewTask, key: string): number[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "number" ? item : Number(item)))
    .filter((item) => Number.isFinite(item) && item > 0);
}

function pagesFromEntries(task: ReviewTask, key: string): number[] {
  const value = task.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => item.page)
    .filter((item): item is number => typeof item === "number" && Number.isFinite(item) && item > 0);
}

export function pagePreviewUrl(jobId: string, pageNumber: number): string {
  return apiUrl(`/jobs/${jobId}/pages/${pageNumber}/preview`);
}

export function previewPagesForTask(task: ReviewTask): number[] {
  const pages = new Set<number>();

  for (const page of metadataPageList(task, "pages_to_check")) {
    if (page > 0) {
      pages.add(page);
    }
  }

  for (const key of ["poor_links", "broken_links", "table_review_targets", "targets", "field_review_targets"]) {
    for (const page of pagesFromEntries(task, key)) {
      if (page > 0) {
        pages.add(page);
      }
    }
  }

  return Array.from(pages).sort((a, b) => a - b).slice(0, 3);
}
