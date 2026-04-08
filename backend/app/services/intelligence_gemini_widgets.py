from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_label, confidence_score
from app.services.intelligence_llm_utils import (
    context_json_part,
    pdf_file_parts,
    preferred_cache_breakpoint_index,
    request_llm_json,
    request_llm_json_with_response,
)
from app.services.llm_client import LlmClient

WIDGET_BATCH_PROMPT = """You are a PDF accessibility widget-rationalization assistant.

You will receive exactly one PDF page and a list of suspicious widget annotations from that page.

Goal:
- decide whether each widget should remain as a real interactive control
- or should be removed because it is only static duplicate content, page chrome, or screenshot/tutorial overlay text

Rules:
- Use `preserve_control` when the widget is a real fillable control, button, checkbox, or other focusable object the user should encounter.
- Use `remove_static_widget` when the widget is only duplicating visible text, page numbers, headers, footers, screenshot callouts, or other non-interactive content.
- Use `manual_only` when the page image and local context are still too ambiguous.
- Prefer `preserve_control` when unsure.
- Do not remove a widget just because it has readable text. Remove it only when the page clearly shows it is not intended as an interactive control.
"""

WIDGET_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["widget_page_intelligence"]},
        "page": {"type": "integer", "minimum": 1},
        "summary": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "field_review_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggested_action": {
                        "type": "string",
                        "enum": ["preserve_control", "remove_static_widget", "manual_only"],
                    },
                    "reason": {"type": "string"},
                },
                "required": [
                    "field_review_id",
                    "summary",
                    "confidence",
                    "suggested_action",
                    "reason",
                ],
            },
        },
    },
    "required": ["task_type", "page", "summary", "decisions"],
}


def _batch_prompt_target(target: dict[str, Any]) -> dict[str, Any]:
    nearby_blocks = []
    for block in list(target.get("nearby_blocks") or [])[:3]:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        nearby_blocks.append(
            {
                "review_id": str(block.get("review_id") or "").strip(),
                "type": str(block.get("type") or "").strip(),
                "text": text[:160],
            }
        )
    return {
        "field_review_id": str(target.get("field_review_id") or "").strip(),
        "bbox": target.get("bbox") if isinstance(target.get("bbox"), dict) else None,
        "field_type": str(target.get("field_type") or "").strip(),
        "field_name": str(target.get("field_name") or "").strip(),
        "accessible_name": str(target.get("accessible_name") or "").strip(),
        "label_quality": str(target.get("label_quality") or "").strip(),
        "value_text": str(target.get("value_text") or "").strip()[:120],
        "suspicion_reasons": list(target.get("suspicion_reasons") or []),
        "same_label_count": int(target.get("same_label_count") or 0),
        "same_label_pages": list(target.get("same_label_pages") or []),
        "looks_like_page_chrome": bool(target.get("looks_like_page_chrome")),
        "page_figure_like_count": int(target.get("page_figure_like_count") or 0),
        "page_table_count": int(target.get("page_table_count") or 0),
        "nearby_blocks": nearby_blocks,
    }


def _normalize_widget_intelligence_result(
    *,
    field_review_id: str,
    page_number: int,
    current_accessible_name: str,
    current_field_name: str,
    raw: dict[str, Any],
    batch_generated: bool = False,
) -> dict[str, Any]:
    suggested_action = (
        str(raw.get("suggested_action") or "manual_only").strip() or "manual_only"
    )
    if suggested_action not in {"preserve_control", "remove_static_widget", "manual_only"}:
        suggested_action = "manual_only"
    confidence = confidence_label(raw.get("confidence"))
    return {
        "task_type": "widget_intelligence",
        "summary": str(raw.get("summary") or "").strip(),
        "confidence": confidence,
        "confidence_score": confidence_score(confidence),
        "suggested_action": suggested_action,
        "reason": str(raw.get("reason") or "").strip(),
        "field_review_id": field_review_id,
        "page": page_number,
        "current_accessible_name": current_accessible_name,
        "current_field_name": current_field_name,
        "batch_generated": batch_generated,
    }


async def generate_widget_intelligence(
    *,
    job: Job | Any,
    target: dict[str, Any],
    llm_client: LlmClient,
) -> dict[str, Any]:
    page_number = int(target.get("page")) if isinstance(target.get("page"), int) else 1
    payload = {
        "job_filename": getattr(job, "original_filename", ""),
        "page": page_number,
        "widgets": [_batch_prompt_target(target)],
    }
    content = [
        {"type": "text", "text": WIDGET_BATCH_PROMPT},
        *pdf_file_parts(job, [page_number], filename=getattr(job, "original_filename", None)),
        context_json_part(payload),
    ]
    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        schema_name="widget_page_intelligence",
        response_schema=WIDGET_BATCH_SCHEMA,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )
    decision = {}
    decisions = parsed.get("decisions")
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            if str(item.get("field_review_id") or "").strip() == str(target.get("field_review_id") or "").strip():
                decision = item
                break
    return _normalize_widget_intelligence_result(
        field_review_id=str(target.get("field_review_id") or "").strip(),
        page_number=page_number,
        current_accessible_name=str(target.get("accessible_name") or "").strip(),
        current_field_name=str(target.get("field_name") or "").strip(),
        raw=decision,
    )


async def generate_widget_intelligence_for_page(
    *,
    job: Job | Any,
    page_number: int,
    targets: list[dict[str, Any]],
    llm_client: LlmClient,
) -> list[dict[str, Any]]:
    if not targets:
        return []

    payload = {
        "job_filename": getattr(job, "original_filename", ""),
        "page": page_number,
        "widgets": [_batch_prompt_target(target) for target in targets],
    }
    content = [
        {"type": "text", "text": WIDGET_BATCH_PROMPT},
        *pdf_file_parts(job, [page_number], filename=getattr(job, "original_filename", None)),
        context_json_part(payload),
    ]
    parsed, _response = await request_llm_json_with_response(
        llm_client=llm_client,
        content=content,
        schema_name="widget_page_intelligence",
        response_schema=WIDGET_BATCH_SCHEMA,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )

    decision_map: dict[str, dict[str, Any]] = {}
    decisions = parsed.get("decisions")
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            field_review_id = str(item.get("field_review_id") or "").strip()
            if not field_review_id:
                continue
            decision_map[field_review_id] = item

    results = [
        _normalize_widget_intelligence_result(
            field_review_id=str(target.get("field_review_id") or "").strip(),
            page_number=page_number,
            current_accessible_name=str(target.get("accessible_name") or "").strip(),
            current_field_name=str(target.get("field_name") or "").strip(),
            raw=decision_map.get(str(target.get("field_review_id") or "").strip()) or {},
            batch_generated=True,
        )
        for target in targets
    ]
    return results
