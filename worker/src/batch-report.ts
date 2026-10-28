/**
 * The batch report: each job's row and section, rendered by the runner with
 * v1's html_report, in the page frame of render_batch_html_report.
 */
import type { BatchParts } from "./contract";

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Python's strftime("%B %d, %Y") in UTC. */
export function reportDate(now: Date): string {
  const day = String(now.getUTCDate()).padStart(2, "0");
  return `${MONTHS[now.getUTCMonth()]} ${day}, ${now.getUTCFullYear()}`;
}

export function batchReport(parts: BatchParts[], now: Date): string | null {
  const first = parts[0];
  if (first === undefined) return null;
  const date = reportDate(now);
  const summaryTable = `
    <table class="batch-summary">
      <thead>
        <tr>
          <th>File</th><th>Status</th><th>Pages</th><th>Remaining issues</th><th>Auto-fixed</th>
        </tr>
      </thead>
      <tbody>
        ${parts.map((part) => part.row).join("")}
      </tbody>
    </table>
    `;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Batch Accessibility Report — ${date}</title>
  ${first.styles}
</head>
<body>
  ${first.header}
  <h1>Batch Accessibility Report</h1>
  <p class="subtitle">${parts.length} ${parts.length === 1 ? "document" : "documents"} &middot; ${date}</p>

  <h2>Summary</h2>
  ${summaryTable}

  ${parts.map((part) => part.section).join("")}
  ${first.footer}
</body>
</html>`;
}
