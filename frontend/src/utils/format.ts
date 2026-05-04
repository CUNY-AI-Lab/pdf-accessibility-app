/**
 * Returns the singular form if count is 1, otherwise the plural form.
 * If no explicit plural is provided, appends "s" to the singular.
 */
export function pluralize(
  count: number,
  singular: string,
  plural?: string,
): string {
  return count === 1 ? singular : (plural ?? `${singular}s`);
}

/**
 * Formats a byte count into a human-readable string (B, KB, or MB).
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatClassification(classification: string): string {
  if (classification === "ocr_scan") return "OCR scan";
  return classification.charAt(0).toUpperCase() + classification.slice(1);
}

/**
 * Formats a date string into a relative time or short date.
 */
export function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  const mins = Math.floor(diff / 60000);

  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/**
 * Formats an array of strings into a human-readable list with an Oxford comma.
 *
 * Examples:
 *   [] -> ""
 *   ["headings"] -> "headings"
 *   ["headings", "tables"] -> "headings and tables"
 *   ["3 headings", "2 tables", "1 list"] -> "3 headings, 2 tables, and 1 list"
 */
export function formatList(items: string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}
