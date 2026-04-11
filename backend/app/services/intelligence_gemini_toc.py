from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.gemini_direct import request_direct_gemini_pdf_json
from app.services.intelligence_gemini_semantics import adjudicate_semantic_unit
from app.services.semantic_units import SemanticUnit

_DEFAULT_ADJUDICATE_SEMANTIC_UNIT = adjudicate_semantic_unit

TOC_GROUP_DIRECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["toc_group_intelligence"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
        "is_toc": {"type": "boolean"},
        "entry_indexes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "entry_types": {
            "type": "object",
            "additionalProperties": {"type": "string", "enum": ["toc_item", "toc_item_table"]},
        },
        "caption_text_override": {"type": "string"},
        "entry_text_overrides": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "reason",
        "is_toc",
        "entry_indexes",
        "entry_types",
        "caption_text_override",
        "entry_text_overrides",
    ],
}

TOC_GROUP_DIRECT_PROMPT = """You are a PDF accessibility table-of-contents assistant.

Decide whether the provided PDF pages contain a real table of contents represented by the supplied candidate group.

Rules:
- Stay grounded in the provided PDF pages and candidate metadata.
- Set is_toc=true only when the visible pages clearly show a table of contents.
- entry_indexes must only reference candidate element indexes from semantic_unit.candidate_elements.
- Use toc_item_table only for candidate elements whose source type is table.
- Use toc_item for heading, paragraph, or list-style entries.
- When visible TOC text is clear, provide entry_text_overrides using short visible labels without leader dots or trailing page numbers.
- Use caption_text_override only when the visible caption should be normalized without changing its meaning.
- If the group is not clearly a TOC, return is_toc=false and empty entry_indexes.
"""


def _default_entry_types(candidate_elements: list[dict[str, Any]], entry_indexes: list[int]) -> dict[str, str]:
    types_by_index = {
        int(item.get("index")): str(item.get("type") or "")
        for item in candidate_elements
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }
    normalized: dict[str, str] = {}
    for entry_index in entry_indexes:
        source_type = types_by_index.get(entry_index, "")
        normalized[str(entry_index)] = "toc_item_table" if source_type == "table" else "toc_item"
    return normalized


def _filter_entry_text_overrides(
    overrides: dict[str, str],
    entry_indexes: list[int],
) -> dict[str, str]:
    if not overrides:
        return {}
    allowed = {str(index) for index in entry_indexes}
    return {
        key: value
        for key, value in overrides.items()
        if key in allowed and str(value or "").strip()
    }


async def generate_toc_group_intelligence(
    *,
    pdf_path,
    original_filename: str,
    candidate_group: dict[str, Any],
    llm_client,
) -> dict[str, Any]:
    pages = [page for page in candidate_group.get("pages", []) if isinstance(page, int) and page > 0]
    candidate_elements = candidate_group.get("candidate_elements") if isinstance(candidate_group.get("candidate_elements"), list) else []
    if adjudicate_semantic_unit is not _DEFAULT_ADJUDICATE_SEMANTIC_UNIT:
        first_page = pages[0] if pages else 1
        decision = await adjudicate_semantic_unit(
            job=SimpleNamespace(
                original_filename=original_filename,
                input_path=str(pdf_path),
                output_path=str(pdf_path),
            ),
            unit=SemanticUnit(
                unit_id=f"toc-group-{candidate_group.get('caption_index')}",
                unit_type="toc_group",
                page=first_page,
                accessibility_goal="Identify table-of-contents entries for PDF accessibility tagging.",
                metadata={
                    "caption_index": candidate_group.get("caption_index"),
                    "caption_text": candidate_group.get("caption_text"),
                    "candidate_elements": candidate_elements,
                    "extra_page_numbers": pages[1:],
                },
            ),
            llm_client=llm_client,
        )
        candidate_indexes = [
            int(item.get("index"))
            for item in candidate_elements
            if isinstance(item, dict) and isinstance(item.get("index"), int)
        ]
        entry_indexes = [
            index for index in decision.entry_indexes if index in set(candidate_indexes)
        ]
        if decision.is_toc and not entry_indexes:
            entry_indexes = candidate_indexes
        entry_types = {
            key: value
            for key, value in decision.entry_types.items()
            if key in {str(index) for index in entry_indexes}
        }
        if entry_indexes and not entry_types:
            entry_types = _default_entry_types(candidate_elements, entry_indexes)
        return {
            "caption_index": int(candidate_group.get("caption_index")) if isinstance(candidate_group.get("caption_index"), int) else -1,
            "is_toc": bool(decision.is_toc),
            "confidence": str(decision.confidence or "").strip().lower() or "low",
            "reason": str(decision.reason or "").strip(),
            "entry_indexes": entry_indexes,
            "entry_types": entry_types,
            "caption_text_override": str(decision.caption_text_override or "").strip(),
            "entry_text_overrides": _filter_entry_text_overrides(
                decision.entry_text_overrides,
                entry_indexes,
            ),
        }

    parsed = await request_direct_gemini_pdf_json(
        pdf_path=pdf_path,
        page_numbers=pages,
        prompt=TOC_GROUP_DIRECT_PROMPT,
        context_payload={
            "original_filename": original_filename,
            "semantic_unit": {
                "unit_id": f"toc-group-{candidate_group.get('caption_index')}",
                "caption_index": candidate_group.get("caption_index"),
                "caption_text": candidate_group.get("caption_text"),
                "candidate_elements": candidate_elements,
            },
        },
        response_schema=TOC_GROUP_DIRECT_SCHEMA,
        system_instruction=(
            "You are evaluating PDF accessibility and TOC semantics. "
            "Stay grounded in the provided PDF pages."
        ),
    )
    entry_indexes = [
        int(item)
        for item in (parsed.get("entry_indexes") or [])
        if isinstance(item, int)
    ]
    entry_types = {
        str(key).strip(): str(value).strip()
        for key, value in (parsed.get("entry_types") or {}).items()
        if str(key).strip() and str(value).strip()
    }
    entry_text_overrides = _filter_entry_text_overrides(
        {
            str(key).strip(): str(value).strip()
            for key, value in (parsed.get("entry_text_overrides") or {}).items()
            if str(key).strip() and str(value).strip()
        },
        entry_indexes,
    )
    if entry_indexes and not entry_types:
        entry_types = _default_entry_types(candidate_elements, entry_indexes)

    return {
        "caption_index": int(candidate_group.get("caption_index")) if isinstance(candidate_group.get("caption_index"), int) else -1,
        "is_toc": bool(parsed.get("is_toc")),
        "confidence": str(parsed.get("confidence") or "").strip().lower() or "low",
        "reason": str(parsed.get("reason") or "").strip(),
        "entry_indexes": entry_indexes,
        "entry_types": entry_types,
        "caption_text_override": str(parsed.get("caption_text_override") or "").strip(),
        "entry_text_overrides": entry_text_overrides,
    }
