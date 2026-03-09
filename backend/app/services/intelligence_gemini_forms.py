from __future__ import annotations

import json
from typing import Any

from app.models import Job
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.intelligence_llm_utils import job_pdf_path, request_llm_json
from app.services.llm_client import LlmClient
from app.services.pdf_preview import render_page_png_data_url
from app.services.semantic_units import SemanticUnit

FORM_BATCH_PROMPT = """You are a PDF accessibility form-label assistant.

You will receive exactly one PDF page preview and a list of form-field candidates from that same page.

Goal:
- choose the accessible field label that assistive technology should announce for each field
- preserve visible meaning
- use the page image and local evidence together

Rules:
- Return one decision per provided field_review_id when possible.
- Prefer concise labels that match the visible label or nearby control text.
- For checkbox and radio controls, combine nearby group text and option text when that is what a screen reader should hear.
- Do not copy long instruction paragraphs verbatim when a shorter faithful label is visible.
- Use confirm_current_label only when the current accessible label is already good.
- Use set_field_label when a better concise label is clearly supported.
- Use manual_only when the field is ambiguous from the page image and nearby context.
"""

FORM_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["form_page_intelligence"]},
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
                        "enum": ["confirm_current_label", "set_field_label", "manual_only"],
                    },
                    "reason": {"type": "string"},
                    "accessible_label": {"type": "string"},
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
    for block in list(target.get("nearby_blocks") or [])[:2]:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        nearby_blocks.append(
            {
                "review_id": str(block.get("review_id") or "").strip(),
                "type": str(block.get("type") or "").strip(),
                "text": text[:120],
            }
        )
    nearby_fields = []
    for field in list(target.get("nearby_fields") or [])[:3]:
        if not isinstance(field, dict):
            continue
        nearby_fields.append(
            {
                "field_review_id": str(field.get("field_review_id") or "").strip(),
                "field_type": str(field.get("field_type") or "").strip(),
                "accessible_name": str(field.get("accessible_name") or "").strip(),
                "label_quality": str(field.get("label_quality") or "").strip(),
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
        "nearby_blocks": nearby_blocks,
        "nearby_fields": nearby_fields,
    }


async def generate_form_intelligence(
    *,
    job: Job,
    target: dict[str, Any],
    nearby_blocks: list[dict[str, Any]],
    llm_client: LlmClient,
) -> dict[str, Any]:
    unit = SemanticUnit(
        unit_id=str(target.get("field_review_id") or "").strip(),
        unit_type="form_field",
        page=int(target.get("page")) if isinstance(target.get("page"), int) else 1,
        accessibility_goal=(
            "Choose the accessible field label that assistive technology should announce, "
            "including group-label context for related controls when needed."
        ),
        bbox=target.get("bbox") if isinstance(target.get("bbox"), dict) else None,
        nearby_context=nearby_blocks,
        current_semantics={
            "accessible_label": str(target.get("accessible_name") or "").strip(),
            "field_name": str(target.get("field_name") or "").strip(),
            "field_type": str(target.get("field_type") or "").strip(),
            "label_quality": str(target.get("label_quality") or "").strip(),
        },
        metadata={
            "field_review_target": target,
            "nearby_fields": list(target.get("nearby_fields") or []),
        },
    )
    decision = await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
    return {
        "task_type": "form_intelligence",
        "summary": decision.summary,
        "confidence": decision.confidence,
        "confidence_score": decision.confidence_score,
        "suggested_action": decision.suggested_action,
        "reason": decision.reason,
        "field_review_id": unit.unit_id,
        "page": unit.page,
        "accessible_label": decision.accessible_label or "",
        "current_accessible_name": str(target.get("accessible_name") or "").strip(),
        "current_field_name": str(target.get("field_name") or "").strip(),
    }


async def generate_form_intelligence_for_page(
    *,
    job: Job,
    page_number: int,
    targets: list[dict[str, Any]],
    llm_client: LlmClient,
) -> list[dict[str, Any]]:
    if not targets:
        return []

    pdf_path = job_pdf_path(job)
    page_preview_url = render_page_png_data_url(pdf_path, page_number)
    payload = {
        "job_filename": getattr(job, "original_filename", ""),
        "page": page_number,
        "fields": [_batch_prompt_target(target) for target in targets],
    }
    content = [
        {"type": "text", "text": FORM_BATCH_PROMPT},
        {"type": "image_url", "image_url": {"url": page_preview_url}},
        {
            "type": "text",
            "text": "Context JSON:\n" f"{json.dumps(payload, indent=2, ensure_ascii=True)}",
        },
    ]
    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        schema_name="form_page_intelligence",
        response_schema=FORM_BATCH_SCHEMA,
        cache_breakpoint_index=1,
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

    normalized: list[dict[str, Any]] = []
    for target in targets:
        field_review_id = str(target.get("field_review_id") or "").strip()
        item = decision_map.get(field_review_id) or {}
        suggested_action = str(item.get("suggested_action") or "manual_only").strip() or "manual_only"
        accessible_label = str(item.get("accessible_label") or "").strip()
        if suggested_action == "set_field_label" and not accessible_label:
            suggested_action = "manual_only"
        normalized.append(
            {
                "task_type": "form_intelligence",
                "summary": str(item.get("summary") or "").strip(),
                "confidence": str(item.get("confidence") or "low").strip() or "low",
                "suggested_action": suggested_action,
                "reason": str(item.get("reason") or "").strip(),
                "field_review_id": field_review_id,
                "page": page_number,
                "accessible_label": accessible_label,
                "current_accessible_name": str(target.get("accessible_name") or "").strip(),
                "current_field_name": str(target.get("field_name") or "").strip(),
                "batch_generated": True,
            }
        )
    return normalized
