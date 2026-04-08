from __future__ import annotations

from typing import Any

from app.pipeline.structure import _collapse_spaced_title_caps
from app.services.gemini_direct import request_direct_gemini_pdf_json
from app.services.llm_client import LlmClient

TITLE_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["document_title_extraction"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "title": {"type": "string"},
    },
    "required": ["task_type", "summary", "confidence", "reason", "title"],
}

TITLE_INTELLIGENCE_PROMPT = """You are a PDF accessibility title extraction assistant.

Decide the best document title for assistive metadata using early-page visible evidence and structural candidates.

Rules:
- Use only visible evidence from the provided PDF pages and the structural candidates.
- Prefer the document's main title, not author lines, institutional boilerplate, dates, running headers, or filenames.
- When the visible title is split across adjacent heading fragments, combine them into one natural title.
- Preserve numbering only when it is visibly part of the title, such as a chapter number.
- Do not invent words that are not visible.
- A current_title may already be present from Docling or metadata. Keep it when it is already the best visible title, but replace it when the broader visible evidence shows a better title.
- If the visible title is too ambiguous to recover faithfully, return an empty title with confidence=low.
"""

TITLE_TEXT_TYPES = {"heading", "paragraph", "list_item", "note"}
TITLE_MAX_PAGES = 2
TITLE_MAX_CANDIDATES = 40


def _title_candidate_elements(structure_json: dict[str, Any]) -> list[dict[str, Any]]:
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

    candidates: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        page_number = element.get("page", -1)
        if not isinstance(page_number, int) or not (0 <= page_number < TITLE_MAX_PAGES):
            continue
        element_type = str(element.get("type") or "").strip()
        if element_type not in TITLE_TEXT_TYPES:
            continue
        raw_text = str(element.get("text") or "").strip()
        text = " ".join(raw_text.split()).strip()
        if not raw_text or not text:
            continue
        candidates.append({
            "index": index,
            "page": page_number + 1,
            "type": element_type,
            "raw_text": raw_text[:400],
            "text": text[:400],
            "bbox": element.get("bbox"),
            "level": element.get("level"),
            "lang": element.get("lang"),
        })
        if len(candidates) >= TITLE_MAX_CANDIDATES:
            break
    return candidates


async def enhance_document_title_with_intelligence(
    *,
    pdf_path,
    structure_json: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_title = _collapse_spaced_title_caps(structure_json.get("title"))
    candidates = _title_candidate_elements(structure_json)
    if not candidates and existing_title:
        structure_json["title"] = existing_title
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "existing_title_without_candidates",
            "title": existing_title,
        }
    if not candidates:
        return structure_json, {
            "attempted": False,
            "applied": False,
            "reason": "no_candidates",
        }

    context_payload = {
        "job_filename": original_filename,
        "title_candidates": candidates,
        "current_title": existing_title,
    }
    parsed = await request_direct_gemini_pdf_json(
        pdf_path=pdf_path,
        page_numbers=list(range(1, TITLE_MAX_PAGES + 1)),
        prompt=TITLE_INTELLIGENCE_PROMPT,
        context_payload=context_payload,
        response_schema=TITLE_DECISION_SCHEMA,
        system_instruction=(
            "You are evaluating PDF accessibility and document title metadata. "
            "Stay grounded in the provided PDF pages."
        ),
    )
    confidence = str(parsed.get("confidence") or "").strip().lower()
    title = _collapse_spaced_title_caps(parsed.get("title"))
    applied = confidence in {"high", "medium"} and bool(title)
    if applied:
        structure_json["title"] = title
    elif existing_title:
        structure_json["title"] = existing_title

    return structure_json, {
        "attempted": True,
        "applied": applied,
        "reason": str(parsed.get("reason") or "").strip(),
        "confidence": confidence,
        "title": title or existing_title,
        "retained_existing_title": bool(existing_title and not applied),
        "candidate_count": len(candidates),
    }
