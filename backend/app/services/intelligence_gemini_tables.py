from __future__ import annotations

import json
from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_score
from app.services.intelligence_llm_utils import job_pdf_path, request_llm_json
from app.services.llm_client import LlmClient
from app.services.pdf_preview import (
    render_bbox_preview_png_data_url,
    render_page_png_data_url,
)

TABLE_INTELLIGENCE_PROMPT = """You are a PDF accessibility table-intelligence assistant.

You will receive:
- a full-page image
- a crop preview of one detected table
- structured metadata for that table

Your goal is to decide whether simple header row and row-header column assignments are sufficient to improve
assistive-technology understanding of the table without changing its visible meaning.

Respond with strict JSON only using this schema:
{
  "task_type": "table_intelligence",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "confirm_current_headers" | "set_table_headers" | "manual_only",
  "reason": "short explanation",
  "table_review_id": "review-7",
  "page": 1,
  "header_rows": [0],
  "row_header_columns": [0]
}

Rules:
- Only use the provided table_review_id and page.
- Recommend header rows and row-header columns only when they clearly improve assistive-tech understanding.
- Preserve visible meaning. Do not invent structure that is not visually supported.
- Multi-level or grouped headers do NOT require manual_only by themselves if the table can still be understood by marking all visible header rows and the appropriate row-header columns.
- Merged cells do NOT require manual_only by themselves if the visible header relationships are still captured by simple header rows and row-header columns.
- Use manual_only only when the visible relationships cannot be conveyed adequately by simple header rows and row-header columns alone.
- Prefer minimal changes.
- Do not include markdown fences or commentary outside the JSON object.
"""

TABLE_INTELLIGENCE_AGGRESSIVE_APPENDIX = """

Aggressive adjudication mode:
- If the visible table relationships can plausibly be conveyed by simple header rows and row-header columns, prefer `set_table_headers` over `manual_only`.
- Do not require perfection. The goal is a faithful first-pass accessible reading, not a visual reconstruction of every spanning cell.
- Use `manual_only` only when simple header rows and row-header columns would clearly misrepresent the table.
"""

TABLE_INTELLIGENCE_CONFIRM_EXISTING_APPENDIX = """

Header confirmation mode:
- The provided current header_rows and row_header_columns are the app's current semantic interpretation.
- If those current headers already provide an acceptable accessible reading of the table, prefer `confirm_current_headers`.
- Only use `set_table_headers` if a different simple header-row / row-header-column assignment is clearly better.
- Use `manual_only` only if neither the current headers nor a simple revised header assignment would be faithful enough.
"""


def _normalize_table_intelligence(parsed: dict[str, Any], *, target: dict[str, Any]) -> dict[str, Any]:
    header_rows = [
        int(value)
        for value in parsed.get("header_rows", [])
        if isinstance(value, int) and value >= 0
    ] if isinstance(parsed.get("header_rows"), list) else []
    row_header_columns = [
        int(value)
        for value in parsed.get("row_header_columns", [])
        if isinstance(value, int) and value >= 0
    ] if isinstance(parsed.get("row_header_columns"), list) else []

    return {
        "task_type": "table_intelligence",
        "summary": str(parsed.get("summary") or "").strip(),
        "confidence": str(parsed.get("confidence") or "low").strip() or "low",
        "confidence_score": confidence_score(parsed.get("confidence")),
        "suggested_action": str(parsed.get("suggested_action") or "manual_only").strip() or "manual_only",
        "reason": str(parsed.get("reason") or "").strip(),
        "table_review_id": str(target.get("table_review_id") or "").strip(),
        "page": int(target.get("page")) if isinstance(target.get("page"), int) else 1,
        "header_rows": sorted(set(header_rows)),
        "row_header_columns": sorted(set(row_header_columns)),
    }


async def generate_table_intelligence(
    *,
    job: Job,
    target: dict[str, Any],
    page_structure_fragments: list[dict[str, Any]],
    llm_client: LlmClient,
    aggressive: bool = False,
    confirm_existing: bool = False,
) -> dict[str, Any]:
    pdf_path = job_pdf_path(job)
    page = int(target.get("page")) if isinstance(target.get("page"), int) else 1
    bbox = target.get("bbox") if isinstance(target.get("bbox"), dict) else None

    images: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {"url": render_page_png_data_url(pdf_path, page)},
        }
    ]
    if bbox:
        try:
            preview_url = render_bbox_preview_png_data_url(pdf_path, page, bbox)
        except Exception:
            preview_url = None
        if preview_url:
            images.append({"type": "image_url", "image_url": {"url": preview_url}})

    payload = {
        "job_filename": job.original_filename,
        "accessibility_goal": (
            "Decide whether simple header-row and row-header-column assignments are enough "
            "for assistive technology to understand this table. Prefer using visible multi-row "
            "headers and row headers when they capture the structure faithfully."
        ),
        "table_review_target": target,
        "page_structure_fragments": page_structure_fragments,
    }
    content = [
        {
            "type": "text",
            "text": (
                f"{TABLE_INTELLIGENCE_PROMPT}"
                f"{TABLE_INTELLIGENCE_AGGRESSIVE_APPENDIX if aggressive else ''}\n\n"
                f"{TABLE_INTELLIGENCE_CONFIRM_EXISTING_APPENDIX if confirm_existing else ''}\n\n"
                "Image order: full-page preview first, then the crop preview for this one table if available.\n\n"
                "Context JSON:\n"
                f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
            ),
        },
        *images,
    ]

    parsed = await request_llm_json(llm_client=llm_client, content=content)
    return _normalize_table_intelligence(parsed, target=target)
