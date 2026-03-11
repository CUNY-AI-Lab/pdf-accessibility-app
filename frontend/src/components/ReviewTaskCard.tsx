import type { ReviewTask } from "../types";
import { pagePreviewUrl, previewPagesForTask } from "../pages/reviewHelpers";
import PreviewImage from "./PreviewImage";

interface ReviewTaskCardProps {
  jobId: string;
  task: ReviewTask;
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return count === 1 ? singular : plural;
}

function metadataNumber(task: ReviewTask, key: string): number {
  const value = task.metadata?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function metadataPages(task: ReviewTask): number[] {
  const value = task.metadata?.pages_to_check;
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => (typeof item === "number" ? item : Number(item)))
    .filter((item) => Number.isFinite(item) && item > 0);
}

function followUpNotes(task: ReviewTask): string[] {
  if (task.task_type === "annotation_description") {
    const poorLinks = Array.isArray(task.metadata?.poor_links) ? task.metadata.poor_links.length : 0;
    const brokenLinks = Array.isArray(task.metadata?.broken_links) ? task.metadata.broken_links.length : 0;
    return [
      poorLinks > 0
        ? `${poorLinks} ${pluralize(poorLinks, "link")} ${pluralize(poorLinks, "uses", "use")} non-descriptive text.`
        : "",
      brokenLinks > 0
        ? `${brokenLinks} internal ${pluralize(brokenLinks, "link")} ${pluralize(brokenLinks, "is", "are")} broken.`
        : "",
    ].filter((item) => item.length > 0);
  }

  if (task.task_type === "alt_text") {
    const machineOnlyAlt = metadataNumber(task, "machine_only_alt");
    const captionBackedAlt = metadataNumber(task, "caption_backed_alt");
    return [
      machineOnlyAlt > 0
        ? `${machineOnlyAlt} generated figure ${pluralize(machineOnlyAlt, "description")} ${pluralize(machineOnlyAlt, "was", "were")} auto-approved without human edits.`
        : "",
      captionBackedAlt > 0
        ? `${captionBackedAlt} figure ${pluralize(captionBackedAlt, "description")} matched visible caption text.`
        : "",
    ].filter((item) => item.length > 0);
  }

  return [];
}

export default function ReviewTaskCard({ jobId, task }: ReviewTaskCardProps) {
  const previewPages = previewPagesForTask(task);
  const pagesToCheck = metadataPages(task);
  const notes = followUpNotes(task);

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-lg text-ink">{task.title}</h3>
          <p className="mt-1 text-sm text-ink-light">{task.detail}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          <span className="rounded-full bg-info-light px-2 py-1 text-[11px] font-medium text-info">
            Optional Check
          </span>
          <span className="rounded-full bg-paper-warm px-2 py-1 text-[11px] text-ink-muted">
            {task.source === "validation" ? "Validation" : "Content"}
          </span>
        </div>
      </div>

      {previewPages.length > 0 && (
        <div className="mt-4">
          {previewPages.slice(0, 1).map((page) => {
            const previewUrl = pagePreviewUrl(jobId, page);
            return (
              <PreviewImage
                key={`${task.id}-preview-${page}`}
                src={previewUrl}
                href={previewUrl}
                alt={`Preview of page ${page}`}
                title={`Page ${page}`}
              />
            );
          })}
        </div>
      )}

      {(pagesToCheck.length > 0 || notes.length > 0) && (
        <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
          {pagesToCheck.length > 0 && (
            <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Pages to check: {pagesToCheck.join(", ")}
            </p>
          )}
          {notes.length > 0 && (
            <div className="mt-3 space-y-2">
              {notes.map((note) => (
                <p key={note} className="text-sm text-ink">
                  {note}
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="mt-4 rounded-lg border border-ink/8 bg-white/70 px-3 py-3">
        <p className="text-sm text-ink">
          This is an optional advanced check if you want extra confidence in the current output.
        </p>
      </div>
    </div>
  );
}
