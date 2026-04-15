"""Generate self-contained HTML accessibility reports."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

from app.models import AltTextEntry, Job, ReviewTask

# ---------------------------------------------------------------------------
# Palette (matches the frontend Tailwind theme)
# ---------------------------------------------------------------------------
_INK = "#1B2A4A"
_INK_MUTED = "#6B7A94"
_ACCENT = "#3B6BCC"
_TEAL = "#2DC4C4"
_CREAM = "#FAF8F5"
_SUCCESS = "#16A34A"
_WARNING = "#D97706"
_ERROR = "#DC2626"


def _e(text: str | None) -> str:
    """HTML-escape a string, defaulting to empty."""
    return html.escape(str(text)) if text else ""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
def _render_styles() -> str:
    return f"""
    <style>
      *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        color: {_INK};
        background: #fff;
        line-height: 1.6;
        font-size: 14px;
        padding: 2rem;
        max-width: 900px;
        margin: 0 auto;
      }}
      h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: .25rem; }}
      h2 {{ font-size: 1.15rem; font-weight: 600; margin: 2rem 0 .75rem; padding-bottom: .4rem; border-bottom: 2px solid {_ACCENT}; }}
      h3 {{ font-size: 1rem; font-weight: 600; margin: 1.25rem 0 .5rem; }}
      .subtitle {{ color: {_INK_MUTED}; font-size: .85rem; }}
      .brand {{ display: flex; align-items: center; gap: .6rem; margin-bottom: 1.5rem; }}
      .brand svg {{ flex-shrink: 0; }}
      .brand-text {{ font-size: .75rem; color: {_INK_MUTED}; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }}

      /* Status badges */
      .badge {{
        display: inline-block;
        padding: .15rem .6rem;
        border-radius: 999px;
        font-size: .75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: .03em;
      }}
      .badge-success {{ background: #DCFCE7; color: {_SUCCESS}; }}
      .badge-warning {{ background: #FEF3C7; color: {_WARNING}; }}
      .badge-error {{ background: #FEE2E2; color: {_ERROR}; }}

      /* Summary grid */
      .stats {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: .75rem;
        margin: 1rem 0;
      }}
      .stat {{
        background: {_CREAM};
        border-radius: .5rem;
        padding: .75rem 1rem;
      }}
      .stat-value {{ font-size: 1.4rem; font-weight: 700; }}
      .stat-label {{ font-size: .75rem; color: {_INK_MUTED}; text-transform: uppercase; letter-spacing: .04em; }}

      /* Tables */
      table {{
        width: 100%;
        border-collapse: collapse;
        margin: .75rem 0;
        font-size: .85rem;
      }}
      th, td {{
        text-align: left;
        padding: .5rem .75rem;
        border-bottom: 1px solid #E5E7EB;
      }}
      th {{
        background: {_CREAM};
        font-weight: 600;
        font-size: .75rem;
        text-transform: uppercase;
        letter-spacing: .04em;
        color: {_INK_MUTED};
      }}
      tr:last-child td {{ border-bottom: none; }}
      .alt-text-cell {{ max-width: 400px; word-wrap: break-word; }}

      /* Issues */
      .issue {{
        background: {_CREAM};
        border-radius: .5rem;
        padding: .75rem 1rem;
        margin: .5rem 0;
        border-left: 3px solid {_INK_MUTED};
      }}
      .issue-blocking {{ border-left-color: {_ERROR}; }}
      .issue-advisory {{ border-left-color: {_WARNING}; }}
      .issue-title {{ font-weight: 600; font-size: .9rem; }}
      .issue-detail {{ color: {_INK_MUTED}; font-size: .85rem; margin-top: .25rem; }}
      .issue-hint {{ font-size: .8rem; color: {_ACCENT}; margin-top: .25rem; font-style: italic; }}

      /* Batch summary */
      .batch-summary {{ margin: 1.5rem 0; }}
      .job-section {{ page-break-inside: avoid; margin-bottom: 2rem; padding-top: 1rem; border-top: 2px solid #E5E7EB; }}
      .job-section:first-of-type {{ border-top: none; padding-top: 0; }}

      /* Footer */
      .footer {{
        margin-top: 3rem;
        padding-top: 1rem;
        border-top: 1px solid #E5E7EB;
        font-size: .75rem;
        color: {_INK_MUTED};
      }}

      /* Print */
      @media print {{
        body {{ padding: 0; font-size: 12px; }}
        .stat {{ break-inside: avoid; }}
        .issue {{ break-inside: avoid; }}
        h2 {{ break-after: avoid; }}
        table {{ break-inside: auto; }}
        tr {{ break-inside: avoid; }}
      }}
    </style>
    """


# ---------------------------------------------------------------------------
# Brand header SVG (the stacked bars mark)
# ---------------------------------------------------------------------------
_BRAND_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="28" height="28">
  <rect x="3" y="4" width="26" height="4" rx="2" fill="#1B2A4A"/>
  <rect x="5" y="10" width="22" height="4" rx="2" fill="#3B6BCC"/>
  <rect x="2" y="16" width="20" height="4" rx="2" fill="#2DC4C4"/>
  <rect x="4" y="22" width="24" height="4" rx="2" fill="#4A7FD4"/>
</svg>"""


def _render_brand_header() -> str:
    return f"""
    <div class="brand">
      {_BRAND_SVG}
      <span class="brand-text">CUNY AI Lab &middot; PDF Accessibility Tool</span>
    </div>
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _status_badge(status: str) -> str:
    if status in ("complete",):
        return '<span class="badge badge-success">Compliant</span>'
    if status in ("manual_remediation",):
        return '<span class="badge badge-warning">Needs Fixes</span>'
    if status in ("failed",):
        return '<span class="badge badge-error">Failed</span>'
    return f'<span class="badge">{_e(status)}</span>'


def _review_status_label(status: str) -> str:
    labels = {
        "kept": "Kept",
        "pending_review": "Pending",
        "undone": "Undone",
        "accepted": "Accepted",
        "rejected": "Decorative",
    }
    return labels.get(status, _e(status))


def _parse_metadata(task: ReviewTask) -> dict[str, Any]:
    if task.metadata_json:
        try:
            return json.loads(task.metadata_json)
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Per-job section renderer
# ---------------------------------------------------------------------------
def _render_job_section(
    job: Job,
    validation: dict[str, Any],
    alt_texts: list[AltTextEntry],
    review_tasks: list[ReviewTask],
    *,
    heading_level: int = 2,
) -> str:
    h = f"h{heading_level}"
    h_sub = f"h{heading_level + 1}"
    parts: list[str] = []

    # --- Header ---
    parts.append(f"""
    <{h}>{_e(job.original_filename)} {_status_badge(job.status)}</{h}>
    <p class="subtitle">
      Processed {_e(job.updated_at.strftime("%B %d, %Y at %I:%M %p") if job.updated_at else "—")}
      &middot; {job.page_count or "?"} pages
      &middot; {_e(job.classification or "unknown")} document
    </p>
    """)

    # --- Tagging summary ---
    tagging: dict[str, Any] = validation.get("tagging", {})
    tag_items = [
        ("Headings", tagging.get("headings_tagged", 0)),
        ("Figures", tagging.get("figures_tagged", 0)),
        ("Tables", tagging.get("tables_tagged", 0)),
        ("Lists", tagging.get("lists_tagged", 0)),
        ("Links", tagging.get("links_tagged", 0)),
        ("Bookmarks", tagging.get("bookmarks_added", 0)),
    ]
    has_tagging = any(v for _, v in tag_items)
    if has_tagging:
        parts.append(f"<{h_sub}>What was done</{h_sub}>")
        parts.append('<div class="stats">')
        for label, value in tag_items:
            if value:
                parts.append(f"""
                <div class="stat">
                  <div class="stat-value">{value}</div>
                  <div class="stat-label">{label} tagged</div>
                </div>
                """)
        # Title / lang
        if tagging.get("title_set"):
            parts.append('<div class="stat"><div class="stat-value">&check;</div><div class="stat-label">Title set</div></div>')
        if tagging.get("lang_set"):
            parts.append('<div class="stat"><div class="stat-value">&check;</div><div class="stat-label">Language set</div></div>')
        parts.append("</div>")

    # --- Semantic coverage ---
    semantic_coverage: dict[str, Any] = validation.get("semantic_coverage", {})
    if semantic_coverage.get("available"):
        interesting_tags = semantic_coverage.get("interesting_tags", {})
        list_numbering = semantic_coverage.get("list_numbering", {})
        semantic_items = [
            ("Struct elements", semantic_coverage.get("total_struct_elems", 0)),
            ("Headings", sum((semantic_coverage.get("heading_tags") or {}).values())),
            ("Lists", interesting_tags.get("L", 0)),
            ("Tables", interesting_tags.get("Table", 0)),
            ("Figures", interesting_tags.get("Figure", 0)),
            ("Captions", interesting_tags.get("Caption", 0)),
            ("Bibliography entries", interesting_tags.get("BibEntry", 0)),
            ("References", interesting_tags.get("Reference", 0)),
        ]
        if any(value for _, value in semantic_items) or list_numbering:
            parts.append(f"<{h_sub}>Semantic coverage</{h_sub}>")
            parts.append('<div class="stats">')
            for label, value in semantic_items:
                if value:
                    parts.append(f"""
                    <div class="stat">
                      <div class="stat-value">{value}</div>
                      <div class="stat-label">{label}</div>
                    </div>
                    """)
            if list_numbering:
                numbering_text = ", ".join(
                    f"{_e(str(name))}: {_e(str(count))}"
                    for name, count in sorted(list_numbering.items())
                )
                parts.append(f"""
                <div class="stat">
                  <div class="stat-value">{len(list_numbering)}</div>
                  <div class="stat-label">List numbering ({numbering_text})</div>
                </div>
                """)
            parts.append("</div>")

    # --- Remediation results ---
    # Rule-level issue counts (distinct accessibility problems), not raw
    # occurrence sums. veraPDF per-rule counts can reach the thousands and
    # are misleading as a single headline number.
    remediation: dict[str, Any] = validation.get("remediation", {})
    baseline_issues = remediation.get("baseline_error_rules", 0)
    post_issues = remediation.get("post_error_rules", 0)
    auto_fixed_issues = remediation.get("auto_remediated_errors", 0)
    auto_fixed = remediation.get("auto_remediated", 0)

    if baseline_issues or post_issues or auto_fixed:
        parts.append(f"<{h_sub}>Remediation results</{h_sub}>")
        parts.append('<div class="stats">')
        parts.append(f"""
        <div class="stat">
          <div class="stat-value">{baseline_issues} &rarr; {post_issues}</div>
          <div class="stat-label">Accessibility issues</div>
        </div>
        <div class="stat">
          <div class="stat-value">{auto_fixed_issues}</div>
          <div class="stat-label">Fixed automatically</div>
        </div>
        <div class="stat">
          <div class="stat-value">{auto_fixed}</div>
          <div class="stat-label">Total rules auto-remediated</div>
        </div>
        """)
        parts.append("</div>")

    # --- Figure descriptions ---
    if alt_texts:
        parts.append(f"<{h_sub}>Figure descriptions</{h_sub}>")
        parts.append("""
        <table>
          <thead>
            <tr><th>Figure</th><th>Description</th><th>Status</th></tr>
          </thead>
          <tbody>
        """)
        for entry in sorted(alt_texts, key=lambda e: e.figure_index):
            desc = entry.edited_text or entry.generated_text
            if entry.status == "rejected" or desc == "decorative":
                display = "<em>Decorative (no alt text)</em>"
            elif desc:
                display = _e(desc)
            else:
                display = "<em>No description</em>"
            parts.append(f"""
            <tr>
              <td>Figure {entry.figure_index + 1}</td>
              <td class="alt-text-cell">{display}</td>
              <td>{_review_status_label(entry.status)}</td>
            </tr>
            """)
        parts.append("</tbody></table>")

    # --- Remaining issues ---
    blocking_tasks = [t for t in review_tasks if t.blocking and t.status != "resolved"]
    advisory_tasks = [t for t in review_tasks if not t.blocking and t.status != "resolved"]

    violations: list[dict[str, Any]] = validation.get("violations", [])
    unresolved_violations = [
        v for v in violations
        if v.get("remediation_status") == "needs_remediation"
    ]

    has_issues = blocking_tasks or advisory_tasks or unresolved_violations
    if has_issues:
        parts.append(f"<{h_sub}>Remaining issues</{h_sub}>")

    if blocking_tasks:
        parts.append(f"<{h_sub}>Blocking issues ({len(blocking_tasks)})</{h_sub}>")
        for task in blocking_tasks:
            metadata = _parse_metadata(task)
            pages = metadata.get("pages_to_check", [])
            pages_str = f" &middot; Pages: {', '.join(str(p) for p in pages)}" if pages else ""
            hint = ""
            if task.detail:
                hint = f'<div class="issue-detail">{_e(task.detail)}</div>'
            parts.append(f"""
            <div class="issue issue-blocking">
              <div class="issue-title">{_e(task.title)}{pages_str}</div>
              {hint}
            </div>
            """)

    if advisory_tasks:
        parts.append(f"<{h_sub}>Advisory ({len(advisory_tasks)})</{h_sub}>")
        for task in advisory_tasks:
            metadata = _parse_metadata(task)
            pages = metadata.get("pages_to_check", [])
            pages_str = f" &middot; Pages: {', '.join(str(p) for p in pages)}" if pages else ""
            parts.append(f"""
            <div class="issue issue-advisory">
              <div class="issue-title">{_e(task.title)}{pages_str}</div>
              <div class="issue-detail">{_e(task.detail)}</div>
            </div>
            """)

    if unresolved_violations:
        parts.append(f"<{h_sub}>Unresolved validation violations ({len(unresolved_violations)})</{h_sub}>")
        parts.append("""
        <table>
          <thead>
            <tr><th>Rule</th><th>Description</th><th>Severity</th><th>Fix hint</th></tr>
          </thead>
          <tbody>
        """)
        for v in unresolved_violations:
            parts.append(f"""
            <tr>
              <td><code>{_e(v.get("rule_id", ""))}</code></td>
              <td>{_e(v.get("description", ""))}</td>
              <td>{_e(v.get("severity", ""))}</td>
              <td>{_e(v.get("fix_hint", "")) or "—"}</td>
            </tr>
            """)
        parts.append("</tbody></table>")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
def _render_footer() -> str:
    now = datetime.now(UTC).strftime("%B %d, %Y at %I:%M %p UTC")
    return f"""
    <div class="footer">
      <p>Generated by CUNY AI Lab PDF Accessibility Tool &middot; {now}</p>
      <p>This report summarizes automated processing. Manual review with assistive
      technology is recommended for full accessibility assurance.</p>
    </div>
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def render_html_report(
    job: Job,
    validation: dict[str, Any],
    alt_texts: list[AltTextEntry],
    review_tasks: list[ReviewTask],
) -> str:
    """Render a self-contained HTML report for a single job."""
    body = _render_job_section(job, validation, alt_texts, review_tasks, heading_level=2)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Accessibility Report — {_e(job.original_filename)}</title>
  {_render_styles()}
</head>
<body>
  {_render_brand_header()}
  <h1>Accessibility Report</h1>
  <p class="subtitle">for {_e(job.original_filename)}</p>
  {body}
  {_render_footer()}
</body>
</html>"""


def render_batch_html_report(
    job_reports: list[dict[str, Any]],
) -> str:
    """Render a combined HTML report for multiple jobs.

    Each entry in *job_reports* must contain:
      job, validation, alt_texts, review_tasks
    """
    now = datetime.now(UTC).strftime("%B %d, %Y")

    # Summary table
    summary_rows: list[str] = []
    for entry in job_reports:
        job: Job = entry["job"]
        validation: dict[str, Any] = entry["validation"]
        remediation = validation.get("remediation", {})
        summary_rows.append(f"""
        <tr>
          <td>{_e(job.original_filename)}</td>
          <td>{_status_badge(job.status)}</td>
          <td>{job.page_count or "?"}</td>
          <td>{remediation.get("post_error_rules", 0)}</td>
          <td>{remediation.get("auto_remediated_errors", 0)}</td>
        </tr>
        """)

    summary_table = f"""
    <table class="batch-summary">
      <thead>
        <tr>
          <th>File</th><th>Status</th><th>Pages</th><th>Remaining issues</th><th>Auto-fixed</th>
        </tr>
      </thead>
      <tbody>
        {"".join(summary_rows)}
      </tbody>
    </table>
    """

    # Per-job sections
    sections: list[str] = []
    for entry in job_reports:
        section = _render_job_section(
            entry["job"],
            entry["validation"],
            entry["alt_texts"],
            entry["review_tasks"],
            heading_level=2,
        )
        sections.append(f'<div class="job-section">{section}</div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Batch Accessibility Report — {now}</title>
  {_render_styles()}
</head>
<body>
  {_render_brand_header()}
  <h1>Batch Accessibility Report</h1>
  <p class="subtitle">{len(job_reports)} {
    "document" if len(job_reports) == 1 else "documents"
  } &middot; {now}</p>

  <h2>Summary</h2>
  {summary_table}

  {"".join(sections)}
  {_render_footer()}
</body>
</html>"""
