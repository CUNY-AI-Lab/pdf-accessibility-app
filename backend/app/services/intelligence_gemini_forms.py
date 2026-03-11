from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_label, confidence_score
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.intelligence_llm_utils import (
    context_json_part,
    page_preview_parts,
    request_llm_json,
)
from app.services.llm_client import LlmClient
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


def _normalize_form_intelligence_result(
    *,
    field_review_id: str,
    page_number: int,
    current_accessible_name: str,
    current_field_name: str,
    raw: dict[str, Any],
    batch_generated: bool = False,
) -> dict[str, Any]:
    suggested_action = str(raw.get("suggested_action") or "manual_only").strip() or "manual_only"
    accessible_label = str(raw.get("accessible_label") or "").strip()
    if suggested_action == "set_field_label" and not accessible_label:
        suggested_action = "manual_only"
    confidence = confidence_label(raw.get("confidence"))
    return {
        "task_type": "form_intelligence",
        "summary": str(raw.get("summary") or "").strip(),
        "confidence": confidence,
        "confidence_score": confidence_score(confidence),
        "suggested_action": suggested_action,
        "reason": str(raw.get("reason") or "").strip(),
        "field_review_id": field_review_id,
        "page": page_number,
        "accessible_label": accessible_label,
        "current_accessible_name": current_accessible_name,
        "current_field_name": current_field_name,
        "batch_generated": batch_generated,
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
    reviewer_feedback: str | None = None,
    previous_intelligence: dict[str, Any] | None = None,
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
            "reviewer_feedback": reviewer_feedback or "",
            "previous_intelligence": previous_intelligence or {},
        },
    )
    decision = await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
    return _normalize_form_intelligence_result(
        field_review_id=unit.unit_id,
        page_number=unit.page,
        current_accessible_name=str(target.get("accessible_name") or "").strip(),
        current_field_name=str(target.get("field_name") or "").strip(),
        raw={
            "summary": decision.summary,
            "confidence": decision.confidence,
            "suggested_action": decision.suggested_action,
            "reason": decision.reason,
            "accessible_label": decision.accessible_label or "",
        },
    )


async def generate_form_intelligence_for_page(
    *,
    job: Job,
    page_number: int,
    targets: list[dict[str, Any]],
    llm_client: LlmClient,
) -> list[dict[str, Any]]:
    if not targets:
        return []

    payload = {
        "job_filename": getattr(job, "original_filename", ""),
        "page": page_number,
        "fields": [_batch_prompt_target(target) for target in targets],
    }
    content = [
        {"type": "text", "text": FORM_BATCH_PROMPT},
        *page_preview_parts(job, [page_number]),
        context_json_part(payload),
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

    return [
        _normalize_form_intelligence_result(
            field_review_id=str(target.get("field_review_id") or "").strip(),
            page_number=page_number,
            current_accessible_name=str(target.get("accessible_name") or "").strip(),
            current_field_name=str(target.get("field_name") or "").strip(),
            raw=decision_map.get(str(target.get("field_review_id") or "").strip()) or {},
            batch_generated=True,
        )
        for target in targets
    ]
