from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_label, confidence_score
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.intelligence_llm_utils import (
    context_json_part,
    pdf_file_parts,
    preferred_cache_breakpoint_index,
    request_llm_json_with_response,
)
from app.services.llm_client import LlmClient
from app.services.semantic_units import SemanticUnit

TABLE_BATCH_PROMPT = """You are a PDF accessibility table-semantics assistant.

You will receive exactly one PDF page, one page-structure excerpt, and a list of table candidates from that same page.

Goal:
- decide whether each table already has sufficient simple header rows and row-header columns
- or whether a revised simple header interpretation is needed for assistive technology

Rules:
- Return one decision per provided table_review_id when possible.
- Use the visible page evidence and the provided table cell preview together.
- Prefer confirm_current_headers when the current simple header interpretation already preserves the visible meaning well enough.
- Use set_table_headers when a better simple header-row / row-header-column interpretation is clearly supported.
- Use manual_only when the visible table appears too complex or ambiguous for a faithful simple-header interpretation.
- Do not invent grouped semantics that are not supported by the visible evidence.
"""

TABLE_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["table_page_intelligence"]},
        "page": {"type": "integer", "minimum": 1},
        "summary": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "table_review_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "suggested_action": {
                        "type": "string",
                        "enum": ["confirm_current_headers", "set_table_headers", "manual_only"],
                    },
                    "reason": {"type": "string"},
                    "header_rows": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                    },
                    "row_header_columns": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                    },
                },
                "required": [
                    "table_review_id",
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


def _normalize_indices(values: Any) -> list[int]:
    normalized: list[int] = []
    if not isinstance(values, list):
        return normalized
    for value in values:
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer >= 0:
            normalized.append(integer)
    return sorted(set(normalized))


def _table_preview_rows(target: dict[str, Any], *, max_rows: int = 4, max_cols: int = 6) -> list[list[str]]:
    rows: dict[int, dict[int, str]] = {}
    for cell in list(target.get("cells") or []):
        if not isinstance(cell, dict):
            continue
        try:
            row = int(cell.get("row"))
            col = int(cell.get("col"))
        except (TypeError, ValueError):
            continue
        if row < 0 or col < 0 or row >= max_rows or col >= max_cols:
            continue
        text = " ".join(str(cell.get("text") or "").split()).strip()
        rows.setdefault(row, {})[col] = text[:80]

    preview: list[list[str]] = []
    for row in sorted(rows.keys())[:max_rows]:
        preview.append([rows[row].get(col, "") for col in range(max_cols)])
    return preview


def _batch_prompt_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "table_review_id": str(target.get("table_review_id") or "").strip(),
        "bbox": target.get("bbox") if isinstance(target.get("bbox"), dict) else None,
        "num_rows": int(target.get("num_rows") or 0),
        "num_cols": int(target.get("num_cols") or 0),
        "header_rows": _normalize_indices(target.get("header_rows")),
        "row_header_columns": _normalize_indices(target.get("row_header_columns")),
        "risk_score": float(target.get("risk_score") or 0.0),
        "risk_reasons": [str(reason).strip() for reason in list(target.get("risk_reasons") or []) if str(reason).strip()],
        "text_excerpt": str(target.get("text_excerpt") or "").strip()[:240],
        "cell_preview_rows": _table_preview_rows(target),
    }


def _normalize_table_intelligence_result(
    *,
    table_review_id: str,
    page_number: int,
    raw: dict[str, Any],
    batch_generated: bool = False,
) -> dict[str, Any]:
    suggested_action = str(raw.get("suggested_action") or "manual_only").strip() or "manual_only"
    if suggested_action not in {"confirm_current_headers", "set_table_headers", "manual_only"}:
        suggested_action = "manual_only"
    confidence = confidence_label(raw.get("confidence"))
    return {
        "task_type": "table_intelligence",
        "summary": str(raw.get("summary") or "").strip(),
        "confidence": confidence,
        "confidence_score": confidence_score(confidence),
        "suggested_action": suggested_action,
        "reason": str(raw.get("reason") or "").strip(),
        "table_review_id": table_review_id,
        "page": page_number,
        "header_rows": _normalize_indices(raw.get("header_rows")),
        "row_header_columns": _normalize_indices(raw.get("row_header_columns")),
        "batch_generated": batch_generated,
    }


async def generate_table_intelligence(
    *,
    job: Job,
    target: dict[str, Any],
    page_structure_fragments: list[dict[str, Any]],
    llm_client: LlmClient,
    aggressive: bool = False,
    confirm_existing: bool = False,
    reviewer_feedback: str | None = None,
    previous_intelligence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unit = SemanticUnit(
        unit_id=str(target.get("table_review_id") or "").strip(),
        unit_type="table",
        page=int(target.get("page")) if isinstance(target.get("page"), int) else 1,
        accessibility_goal=(
            "Decide whether the current or revised simple header rows and row-header columns best "
            "preserve the visible table meaning for assistive technology, including grouped or multi-row headers when they can be represented faithfully."
        ),
        bbox=target.get("bbox") if isinstance(target.get("bbox"), dict) else None,
        structure_context=page_structure_fragments,
        current_semantics={
            "header_rows": list(target.get("header_rows") or []),
            "row_header_columns": list(target.get("row_header_columns") or []),
        },
        metadata={
            "table_review_target": target,
            "aggressive": aggressive,
            "confirm_existing": confirm_existing,
            "reviewer_feedback": reviewer_feedback or "",
            "previous_intelligence": previous_intelligence or {},
        },
    )
    decision = await adjudicate_semantic_unit(
        job=job,
        unit=unit,
        llm_client=llm_client,
    )
    result = {
        "task_type": "table_intelligence",
        "summary": decision.summary,
        "confidence": decision.confidence,
        "confidence_score": decision.confidence_score,
        "suggested_action": decision.suggested_action,
        "reason": decision.reason,
        "table_review_id": unit.unit_id,
        "page": unit.page,
        "header_rows": decision.header_rows,
        "row_header_columns": decision.row_header_columns,
    }
    if decision.resolved_kind:
        result["resolved_kind"] = decision.resolved_kind
    return result


async def generate_table_intelligence_for_page(
    *,
    job: Job,
    page_number: int,
    targets: list[dict[str, Any]],
    page_structure_fragments: list[dict[str, Any]],
    llm_client: LlmClient,
) -> list[dict[str, Any]]:
    if not targets:
        return []

    payload = {
        "job_filename": getattr(job, "original_filename", ""),
        "page": page_number,
        "page_structure_fragments": list(page_structure_fragments or [])[:10],
        "tables": [_batch_prompt_target(target) for target in targets],
    }
    content = [
        {"type": "text", "text": TABLE_BATCH_PROMPT},
        *pdf_file_parts(job, [page_number], filename=getattr(job, "original_filename", None)),
        context_json_part(payload),
    ]
    parsed, _response = await request_llm_json_with_response(
        llm_client=llm_client,
        content=content,
        schema_name="table_page_intelligence",
        response_schema=TABLE_BATCH_SCHEMA,
        cache_breakpoint_index=preferred_cache_breakpoint_index(content),
    )

    decision_map: dict[str, dict[str, Any]] = {}
    decisions = parsed.get("decisions")
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            table_review_id = str(item.get("table_review_id") or "").strip()
            if not table_review_id:
                continue
            decision_map[table_review_id] = item

    results = [
        _normalize_table_intelligence_result(
            table_review_id=str(target.get("table_review_id") or "").strip(),
            page_number=page_number,
            raw=decision_map.get(str(target.get("table_review_id") or "").strip()) or {},
            batch_generated=True,
        )
        for target in targets
    ]
    return results
