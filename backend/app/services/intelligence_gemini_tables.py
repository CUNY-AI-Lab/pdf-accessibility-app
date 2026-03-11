from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.llm_client import LlmClient
from app.services.semantic_units import SemanticUnit


async def generate_table_intelligence(
    *,
    job: Job,
    target: dict[str, Any],
    page_structure_fragments: list[dict[str, Any]],
    llm_client: LlmClient,
    aggressive: bool = False,
    confirm_existing: bool = False,
    reviewer_feedback: str | None = None,
    previous_suggestion: dict[str, Any] | None = None,
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
            "previous_suggestion": previous_suggestion or {},
        },
    )
    decision = await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
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
