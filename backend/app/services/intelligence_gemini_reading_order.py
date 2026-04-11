from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_label, confidence_score
from app.services.intelligence_llm_utils import (
    context_json_part,
    preferred_cache_breakpoint_index,
    request_llm_json,
    semantic_page_parts,
)
from app.services.llm_client import LlmClient

READING_ORDER_INTELLIGENCE_PROMPT = """You are a PDF accessibility reading-order assistant.

You will receive:
- one PDF page
- the current block order for that page
- nearby structure fragments
- suspicious text interpretations when available

Your goal is to decide whether the page order should change so assistive technology reads the page correctly.

Respond with strict JSON only using this schema:
{
  "task_type": "reading_order_intelligence",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "confirm_current_order" | "reorder_review" | "artifact_headers_footers" | "manual_only",
  "reason": "short explanation",
  "page": 1,
  "ordered_review_ids": ["review-3", "review-4"],
  "element_updates": [
    {
      "review_id": "review-5",
      "new_type": "artifact" | "heading" | "paragraph" | "list_item" | "code" | "formula",
      "new_level": 1,
      "reason": "short explanation"
    }
  ]
}

Rules:
- Use only the provided review_id values from page_blocks.
- When reviewer_feedback is present, treat it as a human correction of the previous intelligence output. Follow it when it matches the visible page evidence and accessibility goal.
- If you provide ordered_review_ids, include every page block exactly once.
- Use `artifact_headers_footers` only for repeated running heads, page numbers, or purely decorative side material.
- Use `reorder_review` when the main issue is block order or block role.
- Use `manual_only` when the visual evidence is ambiguous.
- Prefer minimal edits. If only one element needs to be hidden or relabeled, keep ordered_review_ids empty and use element_updates.
- If you set `new_type` to `heading`, provide `new_level` from 1-6. Otherwise omit new_level.
- Keep summaries concise and factual.
- Do not include markdown fences or commentary outside the JSON object.
"""


def _normalize_reading_order_intelligence(
    parsed: dict[str, Any], *, page_number: int
) -> dict[str, Any]:
    ordered_review_ids = (
        [
            str(review_id).strip()
            for review_id in parsed.get("ordered_review_ids", [])
            if str(review_id).strip()
        ]
        if isinstance(parsed.get("ordered_review_ids"), list)
        else []
    )

    element_updates: list[dict[str, Any]] = []
    raw_updates = parsed.get("element_updates")
    if isinstance(raw_updates, list):
        for raw_update in raw_updates:
            if not isinstance(raw_update, dict):
                continue
            review_id = str(raw_update.get("review_id") or "").strip()
            new_type = str(raw_update.get("new_type") or "").strip()
            if not review_id or not new_type:
                continue
            normalized = {
                "review_id": review_id,
                "new_type": new_type,
                "reason": str(raw_update.get("reason") or "").strip(),
            }
            if isinstance(raw_update.get("new_level"), int):
                normalized["new_level"] = int(raw_update["new_level"])
            element_updates.append(normalized)

    return {
        "task_type": "reading_order_intelligence",
        "summary": str(parsed.get("summary") or "").strip(),
        "confidence": confidence_label(parsed.get("confidence")),
        "confidence_score": confidence_score(parsed.get("confidence")),
        "suggested_action": str(parsed.get("suggested_action") or "manual_only").strip()
        or "manual_only",
        "reason": str(parsed.get("reason") or "").strip(),
        "page": page_number,
        "ordered_review_ids": ordered_review_ids,
        "element_updates": element_updates,
    }


async def generate_reading_order_intelligence(
    *,
    job: Job,
    page_number: int,
    page_blocks: dict[str, Any],
    page_structure_fragments: list[dict[str, Any]],
    page_text_intelligence_blocks: list[dict[str, Any]],
    llm_client: LlmClient,
    reviewer_feedback: str | None = None,
    previous_intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "job_filename": job.original_filename,
        "accessibility_goal": (
            "Decide what page reading order and block roles best preserve meaning for "
            "assistive technology users."
        ),
        "page_number": page_number,
        "page_blocks": page_blocks,
        "page_structure_fragments": page_structure_fragments,
        "page_text_intelligence_blocks": page_text_intelligence_blocks,
        "reviewer_feedback": reviewer_feedback or "",
        "previous_intelligence": previous_intelligence or {},
    }
    content = [
        {
            "type": "text",
            "text": (
                f"{READING_ORDER_INTELLIGENCE_PROMPT}\n\nEvidence order: one PDF page input only."
            ),
        },
        *semantic_page_parts(job, [page_number], filename=getattr(job, "original_filename", None)),
        context_json_part(payload),
    ]

    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )
    return _normalize_reading_order_intelligence(parsed, page_number=page_number)
