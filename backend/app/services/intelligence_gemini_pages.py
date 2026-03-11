from __future__ import annotations

from typing import Any

from app.models import Job
from app.services.intelligence_gemini_semantics import adjudicate_semantic_units
from app.services.llm_client import LlmClient
from app.services.semantic_units import SemanticUnit


def _attach_grounding_evidence(
    normalized_blocks: list[dict[str, Any]],
    suspicious_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for block in suspicious_blocks:
        if not isinstance(block, dict):
            continue
        page = block.get("page")
        review_id = str(block.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        evidence_by_key[(page, review_id)] = block

    enriched: list[dict[str, Any]] = []
    for hint in normalized_blocks:
        page = int(hint["page"])
        review_id = str(hint["review_id"])
        evidence = evidence_by_key.get((page, review_id), {})
        enriched.append(
            {
                **hint,
                "role": str(evidence.get("role") or "").strip(),
                "extracted_text": str(evidence.get("extracted_text") or evidence.get("text") or "").strip(),
                "native_text_candidate": str(evidence.get("native_text_candidate") or "").strip(),
                "original_text_candidate": str(
                    evidence.get("original_text_candidate")
                    or evidence.get("native_text_candidate")
                    or ""
                ).strip(),
                "ocr_text_candidate": str(evidence.get("ocr_text_candidate") or "").strip(),
                "previous_text": str(evidence.get("previous_text") or "").strip(),
                "previous_role": str(evidence.get("previous_role") or "").strip(),
                "next_text": str(evidence.get("next_text") or "").strip(),
                "next_role": str(evidence.get("next_role") or "").strip(),
                "signals": list(evidence.get("signals") or []),
            }
        )
    return enriched


async def generate_suspicious_text_intelligence(
    *,
    job: Job,
    page_numbers: list[int],
    suspicious_blocks: list[dict[str, Any]],
    llm_client: LlmClient,
    reviewer_feedback: str | None = None,
    previous_intelligence: dict[tuple[int, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not suspicious_blocks:
        return {
            "task_type": "page_text_intelligence",
            "summary": "",
            "confidence": "low",
            "blocks": [],
        }

    units: list[SemanticUnit] = []
    for block in suspicious_blocks:
        page = block.get("page")
        review_id = str(block.get("review_id") or "").strip()
        if not isinstance(page, int) or page < 1 or not review_id:
            continue
        units.append(
            SemanticUnit(
                unit_id=review_id,
                unit_type="text_block",
                page=page,
                accessibility_goal=(
                    "Infer what assistive technology should probably announce when local extracted text "
                    "looks suspicious or garbled."
                ),
                bbox=block.get("bbox") if isinstance(block.get("bbox"), dict) else None,
                native_text_candidate=str(
                    block.get("native_text_candidate")
                    or block.get("extracted_text")
                    or block.get("text")
                    or ""
                ).strip(),
                ocr_text_candidate=str(block.get("ocr_text_candidate") or "").strip() or None,
                nearby_context=[
                    {
                        "role": str(block.get("previous_role") or "").strip(),
                        "text": str(block.get("previous_text") or "").strip(),
                    },
                    {
                        "role": str(block.get("next_role") or "").strip(),
                        "text": str(block.get("next_text") or "").strip(),
                    },
                ],
                current_semantics={
                    "role": str(block.get("role") or "").strip(),
                    "current_text": str(block.get("extracted_text") or block.get("text") or "").strip(),
                },
                metadata={
                    "signals": list(block.get("signals") or []),
                    "original_text_candidate": str(block.get("original_text_candidate") or "").strip(),
                    "page_numbers_in_scope": page_numbers,
                    "reviewer_feedback": reviewer_feedback or "",
                    "previous_intelligence": (
                        previous_intelligence or {}
                    ).get((page, review_id), {}),
                },
            )
        )

    decisions = await adjudicate_semantic_units(job=job, units=units, llm_client=llm_client)
    normalized_blocks = [
        {
            "page": unit.page,
            "review_id": decision.unit_id,
            "readable_text_hint": decision.resolved_text or "",
            "suggested_action": decision.suggested_action,
            "resolved_kind": decision.resolved_kind,
            "chosen_source": decision.chosen_source or "llm_inferred",
            "issue_type": decision.issue_type or "uncertain",
            "confidence": decision.confidence,
            "should_block_accessibility": decision.should_block_accessibility,
            "reason": decision.reason,
        }
        for unit, decision in zip(units, decisions, strict=False)
        if decision.resolved_text or decision.suggested_action == "mark_decorative"
    ]
    enriched_blocks = _attach_grounding_evidence(normalized_blocks, suspicious_blocks)
    summary = decisions[0].summary if len(decisions) == 1 else f"Reviewed {len(decisions)} suspicious text blocks."
    confidence = decisions[0].confidence if len(decisions) == 1 else (
        "high" if any(decision.confidence == "high" for decision in decisions) else (
            "medium" if any(decision.confidence == "medium" for decision in decisions) else "low"
        )
    )
    confidence_score = max((decision.confidence_score for decision in decisions), default=0.2)
    return {
        "task_type": "page_text_intelligence",
        "summary": summary,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "blocks": enriched_blocks,
    }
