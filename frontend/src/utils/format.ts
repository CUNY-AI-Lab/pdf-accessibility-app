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
