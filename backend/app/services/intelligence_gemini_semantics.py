from __future__ import annotations

import json
from typing import Any

from app.models import Job
from app.services.intelligence_gemini import confidence_score
from app.services.intelligence_llm_utils import job_pdf_path, request_llm_json
from app.services.llm_client import LlmClient
from app.services.pdf_preview import render_bbox_preview_png_data_url, render_page_png_data_url
from app.services.semantic_units import SemanticDecision, SemanticUnit

SEMANTIC_ADJUDICATION_PROMPT = """You are a PDF accessibility semantic adjudication assistant.

You will receive exactly one semantic unit from a PDF. The semantic unit type will be one of:
- text_block
- table
- form_field
- figure
- toc_group

Your goal is to decide the most faithful local accessible interpretation of that region for assistive technology while preserving visible meaning.

Rules:
- Only decide for the provided unit_id and unit_type.
- Preserve visible meaning. Do not invent document content that is not supported by the image and local evidence.
- Use the page image, local crop, native_text_candidate, ocr_text_candidate, nearby_context, structure_context, and metadata together.
- Prefer one of the provided text candidates when it clearly matches the visible content.
- Use chosen_source="llm_inferred" only when neither provided candidate is good enough and the visible content is still locally clear.
- Prefer precision over recall.

Definitions:
- artifact: content that should be hidden from assistive technology because it is purely decorative, redundant, or layout-only and not needed to understand the document.
- form_region: a region that belongs to a larger form/page layout and should not be treated as a standalone figure.
- decorative: visible content that does not add meaning beyond nearby text or structure and should not be announced by assistive technology.
- meaningful: visible content that carries information users need to understand.
- header_rows: zero-based table row indexes that should be announced as column headers for the cells beneath them.
- row_header_columns: zero-based table column indexes that should be announced as row headers for cells to their right.
- group label: text that applies to a set of related controls such as a checkbox or radio group, not just one field.
- faithful accessible reading: what assistive technology should announce so that a user receives the same meaning as a sighted reader, even if the PDF extraction is imperfect.

Text-block rules:
- Use confirm_current_text when the native/current semantic text is already acceptable for assistive technology.
- Use set_resolved_text when a corrected local reading is clearly supported.
- Use mark_decorative when the extracted block is really redundant screenshot/UI text, repeated page furniture, or other non-narrative content that should be hidden from assistive technology instead of being read aloud.
- Use manual_only when the local text is too ambiguous.
- Set should_block_accessibility=true only when the current extracted text would likely mislead assistive technology.
- Use spacing_only when the meaning is clear but spacing is broken.
- Use encoding_problem when characters are materially wrong or garbled.
- For code blocks, preserve visible line breaks and indentation in resolved_text.

Table rules:
- Use confirm_current_headers when the current header_rows and row_header_columns already provide an acceptable accessible reading.
- Use set_table_headers when simple header rows and row-header columns clearly improve assistive-tech understanding.
- Use manual_only only when simple header rows and row-header columns would still clearly misrepresent the table.
- Treat grouped, stacked, or multi-row headers as acceptable for set_table_headers when marking the visible header band and row-header columns would give a faithful accessible reading.
- Multi-row or grouped headers do not require manual_only by themselves if marking all visible header rows and the right row-header columns is faithful enough.
- Prefer a faithful first-pass accessible reading over visual perfection.
- If semantic_unit.metadata.confirm_existing is true, treat the current headers as the default and only change them when a different simple assignment is clearly better.
- If semantic_unit.metadata.aggressive is true, prefer set_table_headers over manual_only whenever a plausible simple accessible reading is still faithful.

Form-field rules:
- Use confirm_current_label when the current accessible label is already good.
- Use set_field_label when one concise accessible label is clearly supported by the visible label and nearby context.
- If the current accessible label is missing, cryptic, or technical, prefer set_field_label when the visible nearby text supports a better label.
- For checkbox and radio controls, use nearby group labels and option text together when they clearly identify what assistive technology should announce.
- When a checkbox or radio button is paired with a long instruction paragraph, prefer a short label that preserves the control's meaning instead of copying the full paragraph verbatim.
- Use manual_only when the field is ambiguous or depends on context you cannot infer confidently.
- Keep form labels concise and factual. Do not add help text unless it is part of the visible label.

Figure rules:
- Use set_alt_text when one concise meaningful description is clearly supported by the image and local context.
- Use mark_decorative only when the image is purely decorative or redundant and should be hidden from assistive technology.
- Use reclassify_region when the current figure candidate is clearly not a standalone figure at all, but rather another content type such as a table, form region, or redundant page region.
- Prefer form_region when the crop is really part of a full form page or widget layout rather than a standalone image.
- Prefer artifact when the crop is just repeated ornament, separator art, or redundant page decoration.
- Use manual_only when the image purpose is unclear or needs a richer human decision.
- Keep alt_text concise and factual. Do not begin with "Image of" or "Picture of".
- When using reclassify_region, set resolved_kind to one of: table, form_region, artifact.
- Do not use a vague catch-all type. If the correct replacement type is not clear enough, use manual_only instead of reclassify_region.

TOC-group rules:
- Use confirm_toc when the candidate group is clearly a table of contents and the current candidate elements already look right as TOC entries.
- Use set_toc_entries when the candidate group is clearly a table of contents and you can identify which candidate element indexes are true TOC entries.
- Use manual_only when it is not clearly a TOC or when the visible relationships are too ambiguous.
- Set is_toc=true only when the visible pages clearly show a table of contents.
- entry_indexes must only reference candidate elements present in semantic_unit.metadata.candidate_elements.
- Use toc_item_table only for candidate elements whose source type is table.
- Use toc_item for heading, paragraph, or list-style entries.
"""

SEMANTIC_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "task_type": {"type": "string", "enum": ["semantic_unit_adjudication"]},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "unit_id": {"type": "string"},
        "unit_type": {
            "type": "string",
            "enum": ["text_block", "table", "form_field", "figure", "toc_group"],
        },
        "suggested_action": {
            "type": "string",
            "enum": [
                "confirm_current_text",
                "set_resolved_text",
                "confirm_current_headers",
                "set_table_headers",
                "confirm_current_label",
                "set_field_label",
                "set_alt_text",
                "mark_decorative",
                "reclassify_region",
                "confirm_toc",
                "set_toc_entries",
                "manual_only",
            ],
        },
        "reason": {"type": "string"},
        "chosen_source": {"type": "string", "enum": ["native", "ocr", "llm_inferred"]},
        "resolved_text": {"type": "string"},
        "issue_type": {"type": "string", "enum": ["spacing_only", "encoding_problem", "uncertain"]},
        "should_block_accessibility": {"type": "boolean"},
        "header_rows": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "row_header_columns": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "accessible_label": {"type": "string"},
        "alt_text": {"type": "string"},
        "resolved_kind": {"type": "string", "enum": ["table", "form_region", "artifact"]},
        "is_decorative": {"type": "boolean"},
        "is_toc": {"type": "boolean"},
        "entry_indexes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "entry_types": {
            "type": "object",
            "additionalProperties": {"type": "string", "enum": ["toc_item", "toc_item_table"]},
        },
    },
    "required": [
        "task_type",
        "summary",
        "confidence",
        "unit_id",
        "unit_type",
        "suggested_action",
        "reason",
    ],
}


def _missing_required_payload(parsed: dict[str, Any]) -> str | None:
    action = str(parsed.get("suggested_action") or "").strip()
    if action == "set_field_label" and not str(parsed.get("accessible_label") or "").strip():
        return "accessible_label"
    if action == "set_alt_text" and not str(parsed.get("alt_text") or "").strip():
        return "alt_text"
    if action == "set_resolved_text" and not str(parsed.get("resolved_text") or "").strip():
        return "resolved_text"
    return None


async def _repair_required_payload(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
    parsed: dict[str, Any],
    missing_field: str,
) -> dict[str, Any]:
    repair_content = list(content)
    repair_prompt = {
        "type": "text",
        "text": (
            "The previous semantic decision chose an action that requires a non-empty payload field. "
            f"Return the same decision again, but populate `{missing_field}` with a concise non-empty value. "
            "Do not change unit_id, unit_type, or suggested_action unless the action truly cannot be completed. "
            "If the action cannot be completed faithfully, return suggested_action=`manual_only` instead.\n\n"
            "Previous parsed JSON:\n"
            f"{json.dumps(parsed, indent=2, ensure_ascii=True)}"
        ),
    }
    repair_content = [repair_content[0], repair_prompt, *repair_content[1:]]
    return await request_llm_json(
        llm_client=llm_client,
        content=repair_content,
        schema_name="semantic_unit_adjudication",
        response_schema=SEMANTIC_DECISION_SCHEMA,
    )


def _normalize_indices(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    return sorted({int(value) for value in values if isinstance(value, int) and value >= 0})


def _normalize_entry_types(values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in values.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text or not value_text:
            continue
        normalized[key_text] = value_text
    return normalized


async def adjudicate_semantic_unit(
    *,
    job: Job | Any | None,
    unit: SemanticUnit,
    llm_client: LlmClient,
) -> SemanticDecision:
    page_images: list[dict[str, Any]] = []
    unit_images: list[dict[str, Any]] = []
    try:
        if job is not None:
            pdf_path = job_pdf_path(job)
            page_images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": render_page_png_data_url(pdf_path, unit.page)},
                }
            )
            extra_pages = unit.metadata.get("extra_page_numbers") if isinstance(unit.metadata, dict) else None
            if isinstance(extra_pages, list):
                for page_number in extra_pages:
                    if isinstance(page_number, int) and page_number > 0 and page_number != unit.page:
                        try:
                            page_images.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": render_page_png_data_url(pdf_path, page_number)},
                                }
                            )
                        except Exception:
                            continue
            if unit.bbox:
                try:
                    preview_url = render_bbox_preview_png_data_url(pdf_path, unit.page, unit.bbox)
                except Exception:
                    preview_url = None
                if preview_url:
                    unit_images.append({"type": "image_url", "image_url": {"url": preview_url}})
    except Exception:
        pass

    extra_image_urls = unit.metadata.get("extra_image_data_urls") if isinstance(unit.metadata, dict) else None
    if isinstance(extra_image_urls, list):
        for url in extra_image_urls:
            if isinstance(url, str) and url.strip():
                unit_images.append({"type": "image_url", "image_url": {"url": url.strip()}})

    payload = {
        "job_filename": getattr(job, "original_filename", "") if job is not None else "",
        "semantic_unit": unit.to_prompt_dict(),
    }
    prompt_text = (
        f"{SEMANTIC_ADJUDICATION_PROMPT}\n\n"
        "Image order: full-page preview first when available, followed by any extra page previews, "
        "then the crop preview for this semantic unit if available, then any unit-specific image previews."
    )
    content = [
        {
            "type": "text",
            "text": prompt_text,
        },
        *page_images,
        {
            "type": "text",
            "text": "Context JSON:\n" f"{json.dumps(payload, indent=2, ensure_ascii=True)}",
        },
        *unit_images,
    ]
    cache_breakpoint_index = len(page_images) if page_images else 0

    parsed = await request_llm_json(
        llm_client=llm_client,
        content=content,
        schema_name="semantic_unit_adjudication",
        response_schema=SEMANTIC_DECISION_SCHEMA,
        cache_breakpoint_index=cache_breakpoint_index,
    )
    missing_field = _missing_required_payload(parsed)
    if missing_field:
        repaired = await _repair_required_payload(
            llm_client=llm_client,
            content=content,
            parsed=parsed,
            missing_field=missing_field,
        )
        if isinstance(repaired, dict):
            parsed = repaired
    return SemanticDecision(
        unit_id=unit.unit_id,
        unit_type=unit.unit_type,
        summary=str(parsed.get("summary") or "").strip(),
        confidence=str(parsed.get("confidence") or "low").strip() or "low",
        confidence_score=confidence_score(parsed.get("confidence")),
        suggested_action=str(parsed.get("suggested_action") or "manual_only").strip() or "manual_only",
        reason=str(parsed.get("reason") or "").strip(),
        chosen_source=str(parsed.get("chosen_source") or "").strip() or None,
        resolved_text=str(parsed.get("resolved_text") or "").strip() or None,
        issue_type=str(parsed.get("issue_type") or "").strip() or None,
        should_block_accessibility=bool(parsed.get("should_block_accessibility", False)),
        header_rows=_normalize_indices(parsed.get("header_rows")),
        row_header_columns=_normalize_indices(parsed.get("row_header_columns")),
        accessible_label=str(parsed.get("accessible_label") or "").strip() or None,
        alt_text=str(parsed.get("alt_text") or "").strip() or None,
        resolved_kind=str(parsed.get("resolved_kind") or "").strip() or None,
        is_decorative=bool(parsed.get("is_decorative", False)),
        is_toc=bool(parsed.get("is_toc", False)),
        entry_indexes=_normalize_indices(parsed.get("entry_indexes")),
        entry_types=_normalize_entry_types(parsed.get("entry_types")),
    )


async def adjudicate_semantic_units(
    *,
    job: Job | Any | None,
    units: list[SemanticUnit],
    llm_client: LlmClient,
) -> list[SemanticDecision]:
    decisions: list[SemanticDecision] = []
    for unit in units:
        decisions.append(
            await adjudicate_semantic_unit(job=job, unit=unit, llm_client=llm_client)
        )
    return decisions
