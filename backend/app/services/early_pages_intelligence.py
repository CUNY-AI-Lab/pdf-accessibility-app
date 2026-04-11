from __future__ import annotations

from typing import Any

from app.pipeline.structure import _collapse_spaced_title_caps
from app.services.bookmark_intelligence import (
    FRONT_MATTER_AUTO_CONFIDENCE,
    _front_matter_page_candidates,
    collect_bookmark_heading_candidates,
)
from app.services.gemini_direct import request_direct_gemini_pdf_json
from app.services.llm_client import LlmClient
from app.services.title_intelligence import _title_candidate_elements

EARLY_PAGES_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["document_early_pages_intelligence"]},
        "summary": {"type": "string"},
        "title": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["confidence", "reason", "title"],
        },
        "front_matter": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "page_index": {"type": "integer", "minimum": 0},
                            "label": {
                                "type": "string",
                                "enum": ["Cover", "Inside-Cover page", "Series Information"],
                            },
                        },
                        "required": ["page_index", "label"],
                    },
                },
            },
            "required": ["confidence", "reason", "entries"],
        },
    },
    "required": ["task_type", "summary", "title", "front_matter"],
}

EARLY_PAGES_INTELLIGENCE_PROMPT = """You are a PDF accessibility early-pages assistant.

You will receive only the early PDF pages that matter for title metadata and pre-TOC front-matter bookmarks,
plus structured candidates extracted from the document.

Return both:
- the best document title for accessible metadata
- any higher-order front-matter bookmark roles that are clearly supported before the visible table of contents

Canonical front-matter role labels:
- Cover
- Inside-Cover page
- Series Information

Rules for title:
- Use only visible evidence from the provided PDF pages and the structural candidates.
- Prefer the document's main title, not author lines, institutional boilerplate, dates, running headers, or filenames.
- When the visible title is split across adjacent heading fragments, combine them into one natural title.
- When adjacent title-block fragments together identify the document, include the identity-bearing fragments; do not drop a visible identifier solely because a descriptive phrase is more prominent.
- Preserve numbering or other identifiers only when they are visibly part of the document title block.
- Do not invent words that are not visible.
- A current_title may already be present from Docling or metadata. Keep it when it is already the best visible title, but replace it when the broader visible evidence shows a better title.
- If the visible title is too ambiguous to recover faithfully, return an empty title with confidence=low.

Rules for front matter:
- Use only the provided canonical role labels.
- Add an entry only when the page's visible content clearly supports that role.
- Prefer Cover for the primary title/cover page.
- Prefer Inside-Cover page for the publication-details/title-verso page that immediately follows a cover.
- Prefer Series Information for an editorial-notes, publication-series, or document-information page that appears before the TOC.
- Return entries in page order.
- Do not invent page roles that are not supported by the visible evidence.
- If none of the pages clearly support these roles, return an empty entries list with confidence=low or medium.
"""


def _normalize_front_matter_entries(
    entries: Any,
    *,
    valid_page_indexes: set[int],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    if not isinstance(entries, list):
        return normalized
    for order_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        page_index = entry.get("page_index")
        if not isinstance(page_index, int):
            continue
        label = str(entry.get("label") or "").strip()
        if page_index not in valid_page_indexes or page_index in seen_pages or not label:
            continue
        normalized.append(
            {
                "candidate_id": f"front:{order_index}",
                "source_kind": "front_matter",
                "source_index": page_index,
                "text": label,
                "page_index": page_index,
                "level": 1,
            }
        )
        seen_pages.add(page_index)
    return normalized


async def enhance_document_title_and_front_matter_with_intelligence(
    *,
    pdf_path,
    structure_json: dict[str, Any],
    original_filename: str,
    llm_client: LlmClient,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    existing_title = _collapse_spaced_title_caps(structure_json.get("title"))
    title_candidates = _title_candidate_elements(structure_json)
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return (
            structure_json,
            {"attempted": False, "applied": False, "reason": "no_elements"},
            [],
            {"attempted": False, "applied": False, "reason": "no_elements", "entry_count": 0},
        )

    candidate_payload = collect_bookmark_heading_candidates(structure_json)
    front_matter_pages = _front_matter_page_candidates(candidate_payload, elements)
    if not title_candidates or not front_matter_pages:
        return (
            structure_json,
            {
                "attempted": False,
                "applied": False,
                "reason": "insufficient_shared_early_page_targets",
            },
            [],
            {
                "attempted": False,
                "applied": False,
                "reason": "insufficient_shared_early_page_targets",
                "entry_count": 0,
            },
        )

    preview_pages = sorted(
        {
            *(candidate["page"] for candidate in title_candidates if isinstance(candidate.get("page"), int)),
            *(page["page_number"] for page in front_matter_pages if isinstance(page.get("page_number"), int)),
        }
    )
    context_payload = {
        "job_filename": original_filename,
        "current_title": existing_title,
        "title_candidates": title_candidates,
        "front_matter_pages": front_matter_pages,
    }
    parsed = await request_direct_gemini_pdf_json(
        pdf_path=pdf_path,
        page_numbers=preview_pages,
        prompt=EARLY_PAGES_INTELLIGENCE_PROMPT,
        context_payload=context_payload,
        response_schema=EARLY_PAGES_INTELLIGENCE_SCHEMA,
        system_instruction=(
            "You are evaluating PDF accessibility and early-page document semantics. "
            "Stay grounded in the provided PDF pages."
        ),
    )

    title_payload = parsed.get("title") if isinstance(parsed.get("title"), dict) else {}
    title_confidence = str(title_payload.get("confidence") or "").strip().lower()
    resolved_title = _collapse_spaced_title_caps(title_payload.get("title"))
    title_applied = title_confidence in {"high", "medium"} and bool(resolved_title)
    if title_applied:
        structure_json["title"] = resolved_title
    elif existing_title:
        structure_json["title"] = existing_title

    front_matter_payload = (
        parsed.get("front_matter") if isinstance(parsed.get("front_matter"), dict) else {}
    )
    front_matter_confidence = str(front_matter_payload.get("confidence") or "").strip().lower()
    valid_page_indexes = {
        int(page["page_index"])
        for page in front_matter_pages
        if isinstance(page.get("page_index"), int)
    }
    front_matter_entries = (
        _normalize_front_matter_entries(
            front_matter_payload.get("entries"),
            valid_page_indexes=valid_page_indexes,
        )
        if front_matter_confidence in FRONT_MATTER_AUTO_CONFIDENCE
        else []
    )

    return (
        structure_json,
        {
            "attempted": True,
            "applied": title_applied,
            "reason": str(title_payload.get("reason") or "").strip(),
            "confidence": title_confidence,
            "title": resolved_title or existing_title,
            "retained_existing_title": bool(existing_title and not title_applied),
            "candidate_count": len(title_candidates),
            "combined_front_matter": True,
        },
        front_matter_entries,
        {
            "attempted": True,
            "applied": bool(front_matter_entries),
            "reason": str(front_matter_payload.get("reason") or "").strip(),
            "confidence": front_matter_confidence,
            "entry_count": len(front_matter_entries),
            "combined_with_title": True,
        },
    )
