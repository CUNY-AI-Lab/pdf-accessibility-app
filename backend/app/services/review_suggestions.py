import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import Job, ReviewTask
from app.services.document_intelligence import (
    build_document_model,
    collect_structure_fragments,
)
from app.services.intelligence_gemini_pages import generate_suspicious_text_intelligence
from app.services.intelligence_gemini_reading_order import generate_reading_order_intelligence
from app.services.intelligence_gemini_tables import generate_table_intelligence
from app.services.intelligence_llm_utils import job_pdf_path, request_llm_json
from app.services.intelligence_merge import document_overlay_for_suggestion
from app.services.intelligence_merge import apply_suspicious_text_intelligence
from app.services.intelligence_merge import apply_table_intelligence
from app.services.llm_client import LlmClient
from app.services.font_unicode_override import inspect_context_font_target
from app.services.page_intelligence import suspicious_text_signals
from app.services.pdf_preview import (
    render_bbox_preview_png_data_url,
    render_page_jpeg_data_url,
    render_target_preview_png_data_url,
)
from app.services.text_grounding import extract_ocr_text_from_bbox

SUPPORTED_SUGGESTION_TASK_TYPES = {"font_text_fidelity", "reading_order", "table_semantics"}
MAX_REVIEW_PAGES = 2
MAX_STRUCTURE_FRAGMENTS = 10
MAX_FONT_TARGET_PREVIEWS = 3
MAX_AUTO_FONT_MAP_TARGETS = 8
MAX_READING_ORDER_BLOCKS_PER_PAGE = 40
MAX_SUSPICIOUS_READING_BLOCKS = 6
MAX_TABLE_TARGETS = 6
AUTO_FONT_MAP_ACTIONS = {"font_map_candidate", "actualtext_candidate"}
AUTO_FONT_ARTIFACT_ACTIONS = {"artifact_if_decorative"}
AUTO_FONT_RESOLUTION_ACTIONS = AUTO_FONT_MAP_ACTIONS | AUTO_FONT_ARTIFACT_ACTIONS
AUTO_FONT_MAP_CONFIDENCE = {"high"}
VISIBLE_GLYPH_HINT_TO_UNICODE = {
    "right pointing triangle": "►",
    "right pointing triangle bullet": "►",
    "right pointing triangle arrowhead": "►",
    "right pointing triangle arrow": "►",
    "triangle bullet": "►",
    "triangular bullet": "►",
    "bullet": "•",
    "black bullet": "•",
    "round bullet": "•",
    "right arrow": "→",
    "left arrow": "←",
}

FONT_REVIEW_PROMPT = """You are assisting manual PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- one or more full-page images from the PDF
- structured metadata about remaining font/Unicode issues

Your job is NOT to invent a remediation. You must help a human reviewer decide what to do next.

Respond with strict JSON only using this schema:
{
  "task_type": "font_text_fidelity",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "manual_only" | "artifact_if_decorative" | "actualtext_candidate" | "font_map_candidate" | "re-run-deterministic-remediation",
  "reason": "short explanation",
  "review_focus": [
    {
      "page": 1,
      "font": "ExampleSymbolFont",
      "operator_index": 17,
      "rule_id": "ISO 14289-1:2014-7.21.7-1",
      "visible_text_hypothesis": "single visible symbol or marker",
      "is_likely_decorative": false,
      "recommended_reviewer_action": "compare visible symbol against spoken/copied text"
    }
  ],
  "actualtext_candidates": [
    {
      "page": 1,
      "operator_index": 17,
      "font": "ExampleSymbolFont",
      "proposed_actualtext": "*",
      "confidence": "medium",
      "reason": "The visible symbol appears to be a single marker glyph."
    }
  ],
  "reviewer_checklist": [
    "bullet one",
    "bullet two"
  ]
}

Rules:
- If the visible meaning is uncertain, set suggested_action to "manual_only".
- Only use "artifact_if_decorative" when the problematic text appears to be purely decorative or a visual ornament.
- Use "font_map_candidate" only when every flagged occurrence appears to be the same localized symbol and a single Unicode character would correctly represent it everywhere that font/code is used.
- Only use "actualtext_candidate" when the visible symbol/text looks semantically important and localized.
- Only include an item in actualtext_candidates when you can tie it to a specific page and operator_index from the provided font_review_targets.
- Leave actualtext_candidates empty if the visible text is uncertain.
- Base any glyph hypothesis on the supplied page images, target crop previews, nearby_text/decoded_text, repeated same-font same-code occurrences, and page_structure_fragments.
- Do not infer a glyph from the font name alone.
- If the repeated occurrence looks like a list marker or decorative pointer and the surrounding structure already conveys that meaning, prefer "artifact_if_decorative" or "manual_only" over "font_map_candidate".
- Keep summaries concise and factual.
- Do not include markdown fences or commentary outside the JSON object.
"""

def _parse_metadata(task: ReviewTask) -> dict[str, Any]:
    if not task.metadata_json:
        return {}
    try:
        data = json.loads(task.metadata_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _single_unicode_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 1 or text.isspace():
        return None
    return text


def _unicode_from_visible_text_hypothesis(value: Any) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if not normalized:
        return None
    return VISIBLE_GLYPH_HINT_TO_UNICODE.get(normalized)

def _document_model(job: Job):
    return build_document_model(job=job)


def _collect_structure_fragments(job: Job) -> list[dict[str, Any]]:
    return collect_structure_fragments(_document_model(job), max_fragments=MAX_STRUCTURE_FRAGMENTS)


def _page_structure_fragments(job: Job, page_numbers: list[int]) -> list[dict[str, Any]]:
    allowed_pages = {page for page in page_numbers if isinstance(page, int) and page > 0}
    if not allowed_pages:
        return []

    fragments: list[dict[str, Any]] = []
    for fragment in _collect_structure_fragments(job):
        page = fragment.get("page")
        if isinstance(page, int) and page in allowed_pages:
            fragments.append(fragment)
    return fragments[:MAX_STRUCTURE_FRAGMENTS]


def _page_blocks_for_review(job: Job, page_numbers: list[int]) -> list[dict[str, Any]]:
    allowed_pages = {page for page in page_numbers if isinstance(page, int) and page > 0}
    if not allowed_pages:
        return []

    document = _document_model(job)
    blocks_by_page: dict[int, list[dict[str, Any]]] = {}
    for page in document.pages:
        if page.page_number not in allowed_pages:
            continue
        for block in page.blocks:
            blocks_by_page.setdefault(page.page_number, []).append(
                {
                    "review_id": block.review_id,
                    "page": page.page_number,
                    "current_order": len(blocks_by_page.get(page.page_number, [])) + 1,
                    "type": block.role,
                    "level": block.level,
                    "text": block.text[:240],
                    "native_text_candidate": block.native_text_candidate[:240],
                    "ocr_text_candidate": block.ocr_text_candidate,
                    "resolved_text": block.resolved_text,
                    "resolution_source": block.resolution_source,
                    "bbox": block.bbox.to_dict() if block.bbox else None,
                    "provenance": block.provenance,
                    "confidence": block.confidence,
                }
            )

    page_blocks: list[dict[str, Any]] = []
    for page in sorted(blocks_by_page):
        blocks = blocks_by_page[page][:MAX_READING_ORDER_BLOCKS_PER_PAGE]
        page_blocks.append({
            "page": page,
            "block_count": len(blocks),
            "blocks": blocks,
        })
    return page_blocks


def _suspicious_reading_blocks(job: Job, page_numbers: list[int]) -> list[dict[str, Any]]:
    suspicious_blocks: list[dict[str, Any]] = []
    pdf_path = job_pdf_path(job)
    page_blocks = _page_blocks_for_review(job, page_numbers)
    for page_block in page_blocks:
        page_entries = [
            block for block in page_block.get("blocks", [])
            if isinstance(block, dict)
        ]
        blocks_by_id = {
            str(block.get("review_id") or "").strip(): block
            for block in page_entries
            if str(block.get("review_id") or "").strip()
        }
        for block in page_block.get("blocks", []):
            if not isinstance(block, dict):
                continue
            text = _normalize_text(block.get("native_text_candidate") or block.get("text"))
            if len(text) < 6:
                continue
            signals = suspicious_text_signals(text)
            if not signals:
                continue
            bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else None
            page = block.get("page")
            ocr_text = ""
            if isinstance(page, int) and isinstance(bbox, dict):
                ocr_text = _normalize_text(
                    extract_ocr_text_from_bbox(
                        pdf_path,
                        page_number=page,
                        bbox=bbox,
                    )
                )
            review_id = str(block.get("review_id") or "").strip()
            current_order = int(block.get("current_order") or 0)
            previous_text = ""
            next_text = ""
            if review_id and current_order > 0:
                previous_block = blocks_by_id.get(
                    str(page_entries[current_order - 2].get("review_id") or "").strip()
                ) if current_order > 1 and current_order - 2 < len(page_entries) else None
                next_block = page_entries[current_order] if current_order < len(page_entries) else None
                if isinstance(previous_block, dict):
                    previous_text = _normalize_text(previous_block.get("native_text_candidate") or previous_block.get("text"))
                if isinstance(next_block, dict):
                    next_text = _normalize_text(next_block.get("native_text_candidate") or next_block.get("text"))
            suspicious_blocks.append({
                "page": page,
                "review_id": review_id,
                "type": block.get("type"),
                "level": block.get("level"),
                "text": text,
                "native_text_candidate": text,
                "ocr_text_candidate": ocr_text,
                "previous_text": previous_text[:240],
                "next_text": next_text[:240],
                "bbox": bbox,
                "signals": signals,
            })
            if len(suspicious_blocks) >= MAX_SUSPICIOUS_READING_BLOCKS:
                return suspicious_blocks
    return suspicious_blocks


def _table_targets_for_review(job: Job, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    requested_pages = {
        page for page in metadata.get("pages_to_check", [])
        if isinstance(page, int) and page > 0
    }
    raw_targets = metadata.get("table_review_targets")
    requested_order = [
        str(target.get("table_review_id")).strip()
        for target in raw_targets if isinstance(target, dict)
        and str(target.get("table_review_id") or "").strip()
    ] if isinstance(raw_targets, list) else []
    requested_ids = set(requested_order)

    document = _document_model(job)
    targets_by_id: dict[str, dict[str, Any]] = {}
    for page in document.pages:
        for table in page.tables:
            review_id = table.table_review_id
            if requested_ids and review_id not in requested_ids:
                continue
            if requested_pages and page.page_number not in requested_pages:
                continue
            if not table.cells:
                continue
            targets_by_id[review_id] = {
                "table_review_id": review_id,
                "page": page.page_number,
                "bbox": table.bbox.to_dict() if table.bbox else None,
                "num_rows": table.num_rows,
                "num_cols": table.num_cols,
                "header_rows": list(table.header_rows),
                "row_header_columns": list(table.row_header_columns),
                "cells": [cell.to_dict() for cell in table.cells],
                "text_excerpt": table.text_excerpt[:240],
                "provenance": table.provenance,
                "confidence": table.confidence,
            }

    if requested_order:
        ordered_targets = [
            targets_by_id[review_id]
            for review_id in requested_order
            if review_id in targets_by_id
        ]
    else:
        ordered_targets = sorted(
            targets_by_id.values(),
            key=lambda item: (int(item["page"]), str(item["table_review_id"])),
        )
    return ordered_targets[:MAX_TABLE_TARGETS]


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _confidence_rank(value: Any) -> int:
    normalized = str(value or "").strip().lower()
    if normalized == "high":
        return 2
    if normalized == "medium":
        return 1
    return 0


def _confidence_label(rank: int) -> str:
    if rank >= 2:
        return "high"
    if rank == 1:
        return "medium"
    return "low"


def _enrich_font_review_targets(pdf_path: Path, raw_targets: list[Any]) -> list[dict[str, Any]]:
    enriched_targets: list[dict[str, Any]] = []
    for target in raw_targets:
        if not isinstance(target, dict):
            continue
        enriched = dict(target)
        context_path = str(target.get("context_path") or "").strip()
        if context_path:
            try:
                font_target = inspect_context_font_target(
                    pdf_path=pdf_path,
                    context_path=context_path,
                )
            except Exception:
                font_target = None
            if isinstance(font_target, dict):
                enriched.update(font_target)
        enriched_targets.append(enriched)
    return enriched_targets


def _group_font_review_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for target in targets:
        font = _normalize_text(target.get("font") or target.get("font_base_name"))
        font_base = _normalize_text(target.get("font_base_name"))
        font_code_hex = _normalize_text(target.get("font_code_hex"))
        key = (font, font_base, font_code_hex)
        entry = grouped.setdefault(
            key,
            {
                "font": font,
                "font_base_name": font_base,
                "font_code_hex": font_code_hex,
                "occurrences": 0,
                "pages": [],
                "sample_decoded_texts": [],
            },
        )
        entry["occurrences"] += 1
        page = target.get("page")
        if isinstance(page, int) and page > 0 and page not in entry["pages"]:
            entry["pages"].append(page)
        decoded = _normalize_text(target.get("decoded_text"))
        if decoded and decoded not in entry["sample_decoded_texts"]:
            entry["sample_decoded_texts"].append(decoded)

    groups = list(grouped.values())
    groups.sort(
        key=lambda item: (
            -int(item.get("occurrences", 0) or 0),
            str(item.get("font") or "").lower(),
            str(item.get("font_code_hex") or ""),
        ),
    )
    return groups


def _font_task_payload(job: Job, task: ReviewTask) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    pages = metadata.get("pages_to_check")
    page_numbers = [
        int(page)
        for page in (pages if isinstance(pages, list) else [])
        if isinstance(page, int) and page > 0
    ][:MAX_REVIEW_PAGES]
    if not page_numbers:
        page_numbers = [1]

    pdf_path = job_pdf_path(job)
    raw_targets = metadata.get("font_review_targets")
    enriched_targets = _enrich_font_review_targets(
        pdf_path,
        raw_targets if isinstance(raw_targets, list) else [],
    )
    images = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": render_page_jpeg_data_url(pdf_path, page_number)},
        })

    target_previews = []
    if enriched_targets:
        for target in enriched_targets[:MAX_FONT_TARGET_PREVIEWS]:
            if not isinstance(target, dict):
                continue
            context_path = str(target.get("context_path") or "").strip()
            page = target.get("page")
            operator_index = target.get("operator_index")
            if not context_path:
                continue
            try:
                preview_url = render_target_preview_png_data_url(pdf_path, context_path)
            except Exception:
                continue
            target_previews.append({
                "page": page,
                "operator_index": operator_index,
                "font": target.get("font"),
            })
            images.append({
                "type": "image_url",
                "image_url": {"url": preview_url},
            })

    payload = {
        "job_filename": job.original_filename,
        "review_task": {
            "task_type": task.task_type,
            "title": task.title,
            "detail": task.detail,
            "severity": task.severity,
            "source": task.source,
        },
        "font_rule_ids": metadata.get("font_rule_ids", []),
        "pages_to_check": page_numbers,
        "fonts_to_check": metadata.get("fonts_to_check", []),
        "font_review_targets": enriched_targets,
        "font_review_groups": _group_font_review_targets(enriched_targets),
        "target_previews": target_previews,
        "page_structure_fragments": _page_structure_fragments(job, page_numbers),
        "unicode_gate": metadata.get("unicode_gate", {}),
        "font_diagnostics_summary": metadata.get("font_diagnostics_summary", {}),
        "top_font_profiles": metadata.get("top_font_profiles", []),
    }
    prompt_text = (
        f"{FONT_REVIEW_PROMPT}\n\n"
        "Image order: full-page previews first, then target crop previews in the same order as target_previews.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


def _reading_order_pages(metadata: dict[str, Any], structure_fragments: list[dict[str, Any]]) -> list[int]:
    page_numbers = [
        page
        for page in metadata.get("pages_to_check", [])
        if isinstance(page, int) and page > 0
    ][:MAX_REVIEW_PAGES]
    if not page_numbers:
        for fragment in structure_fragments:
            page = fragment.get("page")
            if isinstance(page, int) and page > 0 and page not in page_numbers:
                page_numbers.append(page)
            if len(page_numbers) >= MAX_REVIEW_PAGES:
                break
    if not page_numbers:
        page_numbers = [1]
    return page_numbers

def _aggregate_table_suggestions(
    suggestions: list[dict[str, Any]],
    *,
    total_targets: int,
) -> dict[str, Any]:
    proposed_updates: list[dict[str, Any]] = []
    reviewer_checklist: list[str] = []
    reasons: list[str] = []
    summary_parts: list[str] = []
    manual_only_count = 0
    confirm_count = 0
    confidence_rank = 2
    seen_table_ids: set[str] = set()

    for suggestion in suggestions:
        confidence_rank = min(confidence_rank, _confidence_rank(suggestion.get("confidence")))
        action = str(suggestion.get("suggested_action") or "").strip()
        if action == "manual_only":
            manual_only_count += 1
        elif action == "confirm_current_headers":
            confirm_count += 1

        reason = _normalize_text(suggestion.get("reason"))
        if reason:
            reasons.append(reason)

        summary = _normalize_text(suggestion.get("summary"))
        if summary:
            summary_parts.append(summary)

        raw_updates = suggestion.get("proposed_table_updates")
        if isinstance(raw_updates, list):
            for update in raw_updates:
                if not isinstance(update, dict):
                    continue
                table_review_id = _normalize_text(update.get("table_review_id"))
                if not table_review_id or table_review_id in seen_table_ids:
                    continue
                seen_table_ids.add(table_review_id)
                proposed_updates.append(update)

        raw_checklist = suggestion.get("reviewer_checklist")
        if isinstance(raw_checklist, list):
            reviewer_checklist.extend(
                _normalize_text(item)
                for item in raw_checklist
                if _normalize_text(item)
            )

    if proposed_updates:
        suggested_action = "set_table_headers"
    elif manual_only_count:
        suggested_action = "manual_only"
    else:
        suggested_action = "confirm_current_headers"

    proposed_count = len(proposed_updates)
    unresolved_count = max(total_targets - proposed_count - confirm_count, 0)
    if manual_only_count:
        unresolved_count = max(unresolved_count, manual_only_count)

    if proposed_count and unresolved_count:
        summary = (
            f"Reviewed {total_targets} tables; proposed header updates for {proposed_count} "
            f"and left {unresolved_count} for manual review."
        )
    elif proposed_count:
        summary = f"Reviewed {total_targets} tables; proposed header updates for {proposed_count}."
    elif confirm_count == total_targets and total_targets:
        summary = f"Reviewed {total_targets} tables; current header flags look acceptable."
    else:
        summary = f"Reviewed {total_targets} tables; manual review is still required."

    return {
        "task_type": "table_semantics",
        "summary": summary,
        "confidence": _confidence_label(confidence_rank),
        "suggested_action": suggested_action,
        "reason": " ".join(_dedupe_preserving_order(reasons))[:1000],
        "proposed_table_updates": proposed_updates,
        "reviewer_checklist": _dedupe_preserving_order(reviewer_checklist),
        "per_table_summaries": summary_parts,
    }


def _aggregate_table_intelligence(
    intelligence_items: list[dict[str, Any]],
    *,
    total_targets: int,
) -> dict[str, Any]:
    suggestions: list[dict[str, Any]] = []
    for item in intelligence_items:
        if not isinstance(item, dict):
            continue
        suggestions.append(
            {
                "summary": item.get("summary"),
                "confidence": item.get("confidence"),
                "suggested_action": item.get("suggested_action"),
                "reason": item.get("reason"),
                "proposed_table_updates": (
                    [
                        {
                            "page": item.get("page"),
                            "table_review_id": item.get("table_review_id"),
                            "header_rows": item.get("header_rows", []),
                            "row_header_columns": item.get("row_header_columns", []),
                            "reason": item.get("reason"),
                        }
                    ]
                    if str(item.get("suggested_action") or "").strip() == "set_table_headers"
                    else []
                ),
                "reviewer_checklist": [],
            }
        )
    aggregated = _aggregate_table_suggestions(suggestions, total_targets=total_targets)
    aggregated["table_intelligence"] = intelligence_items
    return aggregated


def _aggregate_reading_order_intelligence(
    intelligence_items: list[dict[str, Any]],
    *,
    page_intelligence: dict[str, Any],
) -> dict[str, Any]:
    proposed_page_orders: list[dict[str, Any]] = []
    proposed_element_updates: list[dict[str, Any]] = []
    review_focus: list[dict[str, Any]] = []
    reviewer_checklist: list[str] = [
        "Listen to the affected page in reading order and confirm the sequence matches the visible layout.",
        "Hide only repeated running heads, page numbers, or decorative side material.",
    ]
    reasons: list[str] = []
    summary_parts: list[str] = []
    confidence_rank = 2
    suggested_actions: set[str] = set()

    for item in intelligence_items:
        if not isinstance(item, dict):
            continue
        confidence_rank = min(confidence_rank, _confidence_rank(item.get("confidence")))
        action = _normalize_text(item.get("suggested_action"))
        if action:
            suggested_actions.add(action)
        reason = _normalize_text(item.get("reason"))
        if reason:
            reasons.append(reason)
        summary = _normalize_text(item.get("summary"))
        if summary:
            summary_parts.append(summary)

        page = item.get("page")
        if isinstance(page, int) and page > 0:
            recommended_action = reason or "Check the page order and block roles against the visible layout."
            review_focus.append(
                {
                    "page": page,
                    "font": "",
                    "rule_id": "",
                    "visible_text_hypothesis": summary or f"Review page {page} reading order",
                    "is_likely_decorative": False,
                    "recommended_reviewer_action": recommended_action,
                }
            )

        ordered_review_ids = item.get("ordered_review_ids")
        if isinstance(page, int) and page > 0 and isinstance(ordered_review_ids, list) and ordered_review_ids:
            proposed_page_orders.append(
                {
                    "page": page,
                    "ordered_review_ids": [
                        str(review_id).strip()
                        for review_id in ordered_review_ids
                        if str(review_id).strip()
                    ],
                    "reason": reason,
                }
            )

        raw_updates = item.get("element_updates")
        if isinstance(raw_updates, list):
            for raw_update in raw_updates:
                if not isinstance(raw_update, dict) or not isinstance(page, int) or page < 1:
                    continue
                review_id = _normalize_text(raw_update.get("review_id"))
                new_type = _normalize_text(raw_update.get("new_type"))
                if not review_id or not new_type:
                    continue
                normalized_update = {
                    "page": page,
                    "review_id": review_id,
                    "new_type": new_type,
                    "reason": _normalize_text(raw_update.get("reason")),
                }
                if isinstance(raw_update.get("new_level"), int):
                    normalized_update["new_level"] = int(raw_update["new_level"])
                proposed_element_updates.append(normalized_update)

    if "reorder_review" in suggested_actions:
        suggested_action = "reorder_review"
    elif "artifact_headers_footers" in suggested_actions:
        suggested_action = "artifact_headers_footers"
    elif "manual_only" in suggested_actions:
        suggested_action = "manual_only"
    else:
        suggested_action = "confirm_current_order"

    summaries = _dedupe_preserving_order(summary_parts)
    if summaries:
        summary = " ".join(summaries)[:1000]
    elif suggested_action == "confirm_current_order":
        summary = "Current reading order looks acceptable on the reviewed pages."
    elif suggested_action == "manual_only":
        summary = "Reading order still needs manual review."
    else:
        summary = "Review the proposed reading order changes."

    return {
        "task_type": "reading_order",
        "summary": summary,
        "confidence": _confidence_label(confidence_rank),
        "suggested_action": suggested_action,
        "reason": " ".join(_dedupe_preserving_order(reasons))[:1000],
        "proposed_page_orders": proposed_page_orders,
        "proposed_element_updates": proposed_element_updates,
        "review_focus": review_focus,
        "reviewer_checklist": _dedupe_preserving_order(reviewer_checklist),
        "readable_text_hints": list(page_intelligence.get("blocks", [])),
        "page_text_intelligence": page_intelligence,
        "reading_order_intelligence": intelligence_items,
    }


def _ground_readable_text_hints(
    hints: list[dict[str, Any]] | Any,
    page_intelligence: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(hints, list):
        return list(page_intelligence.get("blocks", []))

    evidence_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for item in page_intelligence.get("blocks", []):
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        review_id = str(item.get("review_id") or "").strip()
        if isinstance(page, int) and page > 0 and review_id:
            evidence_by_key[(page, review_id)] = item

    grounded: list[dict[str, Any]] = []
    for raw_hint in hints:
        if not isinstance(raw_hint, dict):
            continue
        page = raw_hint.get("page")
        review_id = str(raw_hint.get("review_id") or "").strip()
        evidence = evidence_by_key.get((page, review_id), {})
        grounded.append(
            {
                **evidence,
                **raw_hint,
                "extracted_text": str(raw_hint.get("extracted_text") or evidence.get("extracted_text") or "").strip(),
                "native_text_candidate": str(raw_hint.get("native_text_candidate") or evidence.get("native_text_candidate") or "").strip(),
                "ocr_text_candidate": str(raw_hint.get("ocr_text_candidate") or evidence.get("ocr_text_candidate") or "").strip(),
                "chosen_source": str(raw_hint.get("chosen_source") or evidence.get("chosen_source") or "").strip(),
            }
        )
    return grounded


async def generate_review_suggestion(
    *,
    job: Job,
    task: ReviewTask,
    llm_client: LlmClient,
) -> dict[str, Any]:
    if task.task_type not in SUPPORTED_SUGGESTION_TASK_TYPES:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")

    document = _document_model(job)
    if task.task_type == "font_text_fidelity":
        _prompt_text, content = _font_task_payload(job, task)
        suggestion = await request_llm_json(llm_client=llm_client, content=content)
    elif task.task_type == "reading_order":
        metadata = _parse_metadata(task)
        structure_fragments = _collect_structure_fragments(job)
        page_numbers = _reading_order_pages(metadata, structure_fragments)
        suspicious_blocks = _suspicious_reading_blocks(job, page_numbers)
        page_intelligence = await generate_suspicious_text_intelligence(
            job=job,
            page_numbers=page_numbers,
            suspicious_blocks=suspicious_blocks,
            llm_client=llm_client,
        )
        reading_order_intelligence = []
        for page_blocks in _page_blocks_for_review(job, page_numbers):
            page = page_blocks.get("page")
            if not isinstance(page, int) or page < 1:
                continue
            reading_order_intelligence.append(
                await generate_reading_order_intelligence(
                    job=job,
                    page_number=page,
                    page_blocks=page_blocks,
                    page_structure_fragments=_page_structure_fragments(job, [page]),
                    page_text_intelligence_blocks=[
                        item
                        for item in page_intelligence.get("blocks", [])
                        if isinstance(item, dict) and item.get("page") == page
                    ],
                    llm_client=llm_client,
                )
            )
        suggestion = _aggregate_reading_order_intelligence(
            reading_order_intelligence,
            page_intelligence=page_intelligence,
        )
    elif task.task_type == "table_semantics":
        metadata = _parse_metadata(task)
        table_targets = _table_targets_for_review(job, metadata)
        if not table_targets:
            suggestion = {
                "task_type": "table_semantics",
                "summary": "No concrete table targets are available for Gemini review on this task.",
                "confidence": "low",
                "suggested_action": "manual_only",
                "reason": "Table review targets were missing from task metadata.",
                "proposed_table_updates": [],
                "reviewer_checklist": [],
                "table_intelligence": [],
            }
        else:
            per_table_intelligence: list[dict[str, Any]] = []
            for target in table_targets:
                per_table_intelligence.append(
                    await generate_table_intelligence(
                        job=job,
                        target=target,
                        page_structure_fragments=_page_structure_fragments(
                            job,
                            [int(target["page"])] if isinstance(target.get("page"), int) else [1],
                        ),
                        llm_client=llm_client,
                    )
                )
            suggestion = _aggregate_table_intelligence(
                per_table_intelligence,
                total_targets=len(table_targets),
            )
    else:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")
    suggestion.setdefault("task_type", task.task_type)
    if task.task_type == "reading_order" and isinstance(suggestion.get("page_text_intelligence"), dict):
        document, _ = apply_suspicious_text_intelligence(document, suggestion["page_text_intelligence"])
    if task.task_type == "table_semantics" and isinstance(suggestion.get("table_intelligence"), list):
        document, _ = apply_table_intelligence(document, suggestion["table_intelligence"])
    suggestion["document_overlay"] = document_overlay_for_suggestion(document, suggestion)
    suggestion["generated_at"] = datetime.now(UTC).isoformat()
    suggestion["model"] = llm_client.model
    return suggestion


def select_auto_font_review_resolution(
    *,
    job: Job,
    task: ReviewTask,
    suggestion: dict[str, Any],
) -> dict[str, Any] | None:
    if task.task_type != "font_text_fidelity":
        return None

    action = str(suggestion.get("suggested_action") or "").strip()
    confidence = str(suggestion.get("confidence") or "").strip().lower()
    if action not in AUTO_FONT_RESOLUTION_ACTIONS or confidence not in AUTO_FONT_MAP_CONFIDENCE:
        return None

    metadata = _parse_metadata(task)
    font_rule_ids = metadata.get("font_rule_ids")
    if isinstance(font_rule_ids, list):
        normalized_rules = {
            str(rule_id).strip()
            for rule_id in font_rule_ids
            if str(rule_id).strip()
        }
        if not normalized_rules or normalized_rules != {"ISO 14289-1:2014-7.21.7-1"}:
            return None

    raw_targets = metadata.get("font_review_targets")
    if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > MAX_AUTO_FONT_MAP_TARGETS:
        return None

    raw_candidates = suggestion.get("actualtext_candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []

    pdf_path = job_pdf_path(job)
    enriched_targets = _enrich_font_review_targets(pdf_path, raw_targets)
    if len(enriched_targets) != len(raw_targets):
        return None

    target_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    font_names: set[str] = set()
    font_base_names: set[str] = set()
    font_codes: set[str] = set()
    for target in enriched_targets:
        page = target.get("page")
        operator_index = target.get("operator_index")
        if not isinstance(page, int) or not isinstance(operator_index, int):
            return None
        target_code = _normalize_text(target.get("font_code_hex"))
        if not target_code:
            return None
        target_by_pair[(page, operator_index)] = target
        font_codes.add(target_code)
        target_font = _normalize_text(target.get("font"))
        if target_font:
            font_names.add(target_font)
        target_font_base = _normalize_text(target.get("font_base_name"))
        if target_font_base:
            font_base_names.add(target_font_base)

    if len(font_codes) != 1 or len(font_names) > 1 or len(font_base_names) > 1:
        return None

    normalized_candidates: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    suggested_texts: set[str] = set()
    artifact_action = action in AUTO_FONT_ARTIFACT_ACTIONS

    for candidate in candidates:
        if not isinstance(candidate, dict):
            if artifact_action:
                continue
            return None
        page = candidate.get("page")
        operator_index = candidate.get("operator_index")
        if not isinstance(page, int) or not isinstance(operator_index, int):
            if artifact_action:
                continue
            return None
        pair = (page, operator_index)
        if pair in seen_pairs:
            if artifact_action:
                continue
            return None

        target = target_by_pair.get(pair)
        if target is None:
            if artifact_action:
                continue
            return None

        candidate_confidence = str(candidate.get("confidence") or "").strip().lower()
        if candidate_confidence and candidate_confidence not in AUTO_FONT_MAP_CONFIDENCE:
            if artifact_action:
                continue
            return None

        proposed_text = _single_unicode_text(candidate.get("proposed_actualtext"))
        if proposed_text is None:
            if artifact_action:
                continue
            return None

        candidate_font = _normalize_text(candidate.get("font"))
        target_font = _normalize_text(target.get("font"))
        if candidate_font and target_font and candidate_font != target_font:
            if artifact_action:
                continue
            return None

        seen_pairs.add(pair)

        suggested_texts.add(proposed_text)
        if target_font:
            font_names.add(target_font)
        target_font_base = _normalize_text(target.get("font_base_name"))
        if target_font_base:
            font_base_names.add(target_font_base)
        target_code = _normalize_text(target.get("font_code_hex"))
        if not target_code:
            return None
        font_codes.add(target_code)

        normalized_candidates.append({
            "page": page,
            "operator_index": operator_index,
            "context_path": str(target.get("context_path") or "").strip(),
            "font": target_font or candidate_font,
            "font_base_name": target_font_base,
            "font_code_hex": target_code,
            "unicode_text": proposed_text,
        })

    decorative_flags: list[bool] = []
    hypothesis_unicode: set[str] = set()
    review_focus = suggestion.get("review_focus")
    if isinstance(review_focus, list):
        for item in review_focus:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            operator_index = item.get("operator_index")
            if not isinstance(page, int) or not isinstance(operator_index, int):
                continue
            if (page, operator_index) not in target_by_pair:
                continue
            decorative_value = item.get("is_likely_decorative")
            if isinstance(decorative_value, bool):
                decorative_flags.append(decorative_value)
            hinted_unicode = _unicode_from_visible_text_hypothesis(
                item.get("visible_text_hypothesis")
            )
            if hinted_unicode:
                hypothesis_unicode.add(hinted_unicode)

    if action in AUTO_FONT_ARTIFACT_ACTIONS:
        if not decorative_flags or not all(decorative_flags):
            return None
        artifact_targets = [
            {
                "page_number": int(target["page"]),
                "operator_index": int(target["operator_index"]),
                "context_path": str(target.get("context_path") or ""),
            }
            for target in sorted(
                target_by_pair.values(),
                key=lambda item: (int(item["page"]), int(item["operator_index"])),
            )
        ]
        if any(not target["context_path"] for target in artifact_targets):
            return None
        unicode_text = ""
        if seen_pairs == set(target_by_pair) and len(suggested_texts) == 1:
            unicode_text = str(next(iter(suggested_texts)))
        elif len(hypothesis_unicode) == 1:
            unicode_text = str(next(iter(hypothesis_unicode)))
        sample_target = target_by_pair[sorted(target_by_pair.keys())[0]]
        return {
            "resolution_type": "artifact",
            "font": str(sample_target.get("font") or ""),
            "font_base_name": str(sample_target.get("font_base_name") or ""),
            "font_code_hex": str(sample_target.get("font_code_hex") or ""),
            "unicode_text": unicode_text,
            "target_count": len(artifact_targets),
            "targets": artifact_targets,
        }

    if len(candidates) != len(raw_targets):
        return None
    if seen_pairs != set(target_by_pair):
        return None
    if len(suggested_texts) != 1:
        return None

    selected = normalized_candidates[0]
    if any(decorative_flags):
        return None

    return {
        "resolution_type": "font_map",
        "page_number": int(selected["page"]),
        "operator_index": int(selected["operator_index"]),
        "unicode_text": str(selected["unicode_text"]),
        "font": str(selected.get("font") or ""),
        "font_base_name": str(selected.get("font_base_name") or ""),
        "font_code_hex": str(selected.get("font_code_hex") or ""),
        "target_count": len(normalized_candidates),
    }


def select_auto_font_map_override(
    *,
    job: Job,
    task: ReviewTask,
    suggestion: dict[str, Any],
) -> dict[str, Any] | None:
    selected = select_auto_font_review_resolution(
        job=job,
        task=task,
        suggestion=suggestion,
    )
    if not isinstance(selected, dict) or str(selected.get("resolution_type") or "") != "font_map":
        return None
    return {
        "page_number": int(selected["page_number"]),
        "operator_index": int(selected["operator_index"]),
        "unicode_text": str(selected["unicode_text"]),
        "font": str(selected.get("font") or ""),
        "font_base_name": str(selected.get("font_base_name") or ""),
        "font_code_hex": str(selected.get("font_code_hex") or ""),
        "target_count": int(selected.get("target_count", 1) or 1),
    }
