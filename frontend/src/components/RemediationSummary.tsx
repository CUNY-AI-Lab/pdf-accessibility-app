import type { ValidationReport } from "../types";
import { formatList, pluralize } from "../utils/format";
import { asBool, asNumber } from "../utils/typeGuards";

type IconName = "wrench" | "scan" | "layout" | "image" | "eye-off" | "bookmark" | "link" | "file-text";

interface RemediationSummaryProps {
  report: ValidationReport;
  classification?: "scanned" | "digital" | "mixed" | "ocr_scan";
}

export default function RemediationSummary({
  report,
  classification,
}: RemediationSummaryProps) {
  const remediation =
    report.remediation && typeof report.remediation === "object"
      ? (report.remediation as Record<string, unknown>)
      : {};
  const tagging =
    report.tagging && typeof report.tagging === "object"
      ? (report.tagging as Record<string, unknown>)
      : {};

  const lines: { icon: IconName; text: string }[] = [];

  // Error remediation summary — count distinct rule-level issues, not raw
  // occurrence totals. veraPDF reports per-rule occurrence counts that can
  // easily reach the thousands, which reads as alarming noise.
  const autoRemediatedErrors = asNumber(remediation.auto_remediated_errors);
  const baselineErrorRules = asNumber(remediation.baseline_error_rules);
  if (
    autoRemediatedErrors !== null &&
    autoRemediatedErrors > 0 &&
    baselineErrorRules !== null &&
    baselineErrorRules > 0
  ) {
    lines.push({
      icon: "wrench",
      text: `Fixed ${autoRemediatedErrors} of ${baselineErrorRules} accessibility ${pluralize(baselineErrorRules, "issue")} automatically`,
    });
  }

  // OCR
  if (
    classification === "scanned" ||
    classification === "mixed" ||
    classification === "ocr_scan"
  ) {
    lines.push({
      icon: "scan",
      text:
        classification === "scanned"
          ? "Extracted text from scanned pages using OCR"
          : classification === "mixed"
            ? "Extracted text from scanned sections using OCR"
            : "Used the existing OCR text layer from scanned pages",
    });
  }

  // Structure tagging — combine headings, tables, lists into one line
  const headings = asNumber(tagging.headings_tagged);
  const tables = asNumber(tagging.tables_tagged);
  const lists = asNumber(tagging.lists_tagged);
  const structureParts: string[] = [];
  if (headings && headings > 0) structureParts.push(`${headings} ${pluralize(headings, "heading")}`);
  if (tables && tables > 0) structureParts.push(`${tables} ${pluralize(tables, "table")}`);
  if (lists && lists > 0) structureParts.push(`${lists} ${pluralize(lists, "list")}`);
  if (structureParts.length > 0) {
    lines.push({
      icon: "layout",
      text: `Tagged ${formatList(structureParts)} for document structure`,
    });
  }

  // Alt text for figures
  const figures = asNumber(tagging.figures_tagged);
  if (figures && figures > 0) {
    lines.push({
      icon: "image",
      text: `Described ${figures} ${pluralize(figures, "image")} with alt text`,
    });
  }

  // Decorative images
  const decorative = asNumber(tagging.decorative_figures_artifacted);
  if (decorative && decorative > 0) {
    lines.push({
      icon: "eye-off",
      text: `Marked ${decorative} decorative ${pluralize(decorative, "image")} as artifacts`,
    });
  }

  // Bookmarks
  const bookmarks = asNumber(tagging.bookmarks_added);
  if (bookmarks && bookmarks > 0) {
    lines.push({
      icon: "bookmark",
      text: `Added ${bookmarks} ${pluralize(bookmarks, "bookmark")} for navigation`,
    });
  }

  // Links
  const links = asNumber(tagging.links_tagged);
  if (links && links > 0) {
    lines.push({
      icon: "link",
      text: `Tagged ${links} ${pluralize(links, "link")}`,
    });
  }

  // Document metadata
  const titleSet = asBool(tagging.title_set);
  const langSet = asBool(tagging.lang_set);
  if (titleSet || langSet) {
    const parts: string[] = [];
    if (titleSet) parts.push("title");
    if (langSet) parts.push("language");
    lines.push({
      icon: "file-text",
      text: `Set document ${formatList(parts)}`,
    });
  }

  if (lines.length === 0) return null;

  return (
    <div className="rounded-xl border border-ink/6 bg-cream p-5 animate-slide-up">
      <h3 className="font-display text-lg text-ink mb-3">
        What we did
      </h3>
      <ul className="space-y-2.5">
        {lines.map((line) => (
          <li key={line.text} className="flex items-start gap-3">
            <span className="w-7 h-7 rounded-lg bg-paper-warm text-ink-muted flex items-center justify-center shrink-0 mt-0.5">
              <LineIcon name={line.icon} />
            </span>
            <span className="text-sm text-ink leading-relaxed">{line.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function LineIcon({ name }: { name: IconName }) {
  const props = {
    width: 14,
    height: 14,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  switch (name) {
    case "wrench":
      return (
        <svg {...props}>
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
      );
    case "scan":
      return (
        <svg {...props}>
          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
    case "layout":
      return (
        <svg {...props}>
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
          <line x1="3" y1="9" x2="21" y2="9" />
          <line x1="9" y1="21" x2="9" y2="9" />
        </svg>
      );
    case "image":
      return (
        <svg {...props}>
          <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
          <circle cx="8.5" cy="8.5" r="1.5" />
          <polyline points="21 15 16 10 5 21" />
        </svg>
      );
    case "eye-off":
      return (
        <svg {...props}>
          <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
          <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
          <line x1="1" y1="1" x2="23" y2="23" />
        </svg>
      );
    case "bookmark":
      return (
        <svg {...props}>
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
        </svg>
      );
    case "link":
      return (
        <svg {...props}>
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
      );
    case "file-text":
      return (
        <svg {...props}>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="16" y1="13" x2="8" y2="13" />
          <line x1="16" y1="17" x2="8" y2="17" />
        </svg>
      );
    default:
      return (
        <svg {...props}>
          <circle cx="12" cy="12" r="10" />
        </svg>
      );
  }
}
