import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import Job, ReviewTask
from app.services.llm_client import LlmClient
from app.services.font_unicode_override import inspect_context_font_target
from app.services.page_intelligence import suspicious_text_signals
from app.services.pdf_preview import (
    render_bbox_preview_png_data_url,
    render_page_png_data_url,
    render_target_preview_png_data_url,
)

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

READING_ORDER_PROMPT = """You are assisting manual PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- one or more full-page images from the PDF
- cropped previews of suspicious text blocks when available
- sampled structural elements in the order our pipeline extracted them
- reading-order metrics from the fidelity gate

Your job is NOT to rewrite the PDF. You must help a human reviewer decide whether the order looks acceptable or needs manual correction, and help interpret any suspicious extracted text so that assistive technology would announce the right content.

Respond with strict JSON only using this schema:
{
  "task_type": "reading_order",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "confirm_current_order" | "reorder_review" | "artifact_headers_footers" | "manual_only",
  "reason": "short explanation",
  "proposed_page_orders": [
    {
      "page": 1,
      "ordered_review_ids": ["review-3", "review-4", "review-5"],
      "reason": "short explanation"
    }
  ],
  "proposed_element_updates": [
    {
      "page": 1,
      "review_id": "review-5",
      "new_type": "artifact" | "heading" | "paragraph" | "list_item" | "code" | "formula",
      "new_level": 1,
      "reason": "short explanation"
    }
  ],
  "review_focus": [
    {
      "page": 1,
      "font": "",
      "rule_id": "",
      "visible_text_hypothesis": "sidebar appears before main paragraph",
      "is_likely_decorative": false,
      "recommended_reviewer_action": "check whether the sidebar should be artifacted or moved after the body text"
    }
  ],
  "reviewer_checklist": [
    "bullet one",
    "bullet two"
  ],
  "readable_text_hints": [
    {
      "page": 1,
      "review_id": "review-5",
      "extracted_text": "D a t a  B o o k",
      "readable_text_hint": "Data Book",
      "issue_type": "spacing_only" | "encoding_problem" | "uncertain",
      "confidence": "high" | "medium" | "low",
      "should_block_accessibility": true,
      "reason": "short explanation"
    }
  ]
}

Rules:
- Use "confirm_current_order" only when the current order looks acceptable from the page images and sampled structure.
- Use "artifact_headers_footers" only for repeated running heads, page numbers, or purely decorative side material.
- Use "reorder_review" when the issue appears to be structural ordering, not missing text.
- Use "manual_only" when the visual evidence is ambiguous.
- Only use review_id values that appear in the provided page_blocks data.
- Only use readable_text_hints for blocks listed in suspicious_text_blocks.
- The readable_text_hint should state what a screen reader user should probably hear, not what the PDF currently extracts.
- If the visible text is unclear, set issue_type to "uncertain" and keep confidence low.
- For proposed_page_orders, include a page only if ordered_review_ids contains every block on that page exactly once.
- Prefer minimal edits. If only one element is decorative, use proposed_element_updates instead of rewriting the whole page order.
- If you propose a heading, set new_level to 1-6. Otherwise omit new_level.
- Do not invent blocks, pages, or tags that are not present in the provided context.
- Keep summaries concise and factual.
- Do not include markdown fences or commentary outside the JSON object.
"""

TABLE_REVIEW_PROMPT = """You are assisting manual PDF accessibility remediation for a PDF/UA workflow.

You will receive:
- a full-page image from the PDF
- a cropped preview of one specific detected table
- structured table metadata including cells, spans, and current header flags
- nearby page structure fragments for context

Your job is NOT to rewrite the PDF. You must help a human reviewer decide whether the current table headers are acceptable or need correction so that assistive technology can understand the table correctly.

Respond with strict JSON only using this schema:
{
  "task_type": "table_semantics",
  "summary": "short summary",
  "confidence": "high" | "medium" | "low",
  "suggested_action": "confirm_current_headers" | "set_table_headers" | "manual_only",
  "reason": "short explanation",
  "proposed_table_updates": [
    {
      "page": 1,
      "table_review_id": "review-7",
      "header_rows": [0],
      "row_header_columns": [0],
      "reason": "short explanation"
    }
  ],
  "reviewer_checklist": [
    "bullet one",
    "bullet two"
  ]
}

Rules:
- The goal is accessibility and faithful meaning preservation, not merely clearing a validator rule.
- Recommend header rows and row-header columns only when they will help screen readers announce the table correctly.
- Preserve the visible meaning and relationships in the source table. Do not simplify away meaningful structure.
- If merged cells, multi-level headers, grouped sections, or layout ambiguity mean that simple header_rows/row_header_columns are not enough, use "manual_only".
- Only use the table_review_id value that appears in the provided table_review_target.
- header_rows and row_header_columns are zero-based indices.
- Use "confirm_current_headers" only when the current header flags already look correct.
- Use "set_table_headers" only when the table appears regular enough that header rows/columns can be identified confidently from the crop and cell text.
- Use "manual_only" when the table is highly irregular, ambiguous, or depends on semantics you cannot infer confidently.
- Prefer minimal changes and keep arrays short.
- Do not invent tables, rows, columns, cells, totals, or relationships that are not present in the provided context.
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


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].startswith("```"):
            text = "\n".join(lines[1:-1]).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")

    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON was not an object")
    return parsed


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


def _parse_job_structure(job: Job) -> dict[str, Any]:
    if not job.structure_json:
        return {}
    try:
        parsed = json.loads(job.structure_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _render_page_image(pdf_path: Path, page_number: int) -> str:
    return render_page_png_data_url(pdf_path, page_number)


def _job_pdf_path(job: Job) -> Path:
    candidates = []
    if getattr(job, "output_path", None):
        candidates.append(Path(str(job.output_path)))
    if getattr(job, "input_path", None):
        candidates.append(Path(str(job.input_path)))

    for pdf_path in candidates:
        if pdf_path.exists():
            return pdf_path

    preferred = candidates[0] if candidates else None
    raise RuntimeError(f"PDF file not found for review suggestion: {preferred}")


def _collect_structure_fragments(job: Job) -> list[dict[str, Any]]:
    structure_json = _parse_job_structure(job)
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

    fragments: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = _normalize_text(element.get("text"))
        if len(text) < 16:
            continue
        page_raw = element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        element_type = str(element.get("type") or "").strip()
        key = (page or 0, element_type, text[:120])
        if key in seen:
            continue
        seen.add(key)
        fragments.append({
            "page": page,
            "type": element_type,
            "text": text[:240],
            "bbox": element.get("bbox") if isinstance(element.get("bbox"), dict) else None,
        })
        if len(fragments) >= MAX_STRUCTURE_FRAGMENTS:
            break
    return fragments


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
    structure_json = _parse_job_structure(job)
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

    allowed_pages = {page for page in page_numbers if isinstance(page, int) and page > 0}
    if not allowed_pages:
        return []

    blocks_by_page: dict[int, list[dict[str, Any]]] = {}
    for index, raw_element in enumerate(elements):
        if not isinstance(raw_element, dict):
            continue
        page_raw = raw_element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        if page is None or page not in allowed_pages:
            continue
        review_id = str(raw_element.get("review_id") or f"review-{index}")
        block = {
            "review_id": review_id,
            "page": page,
            "current_order": len(blocks_by_page.get(page, [])) + 1,
            "type": str(raw_element.get("type") or "paragraph"),
            "level": int(raw_element.get("level")) if isinstance(raw_element.get("level"), int) else None,
            "text": _normalize_text(raw_element.get("text"))[:240],
            "bbox": raw_element.get("bbox") if isinstance(raw_element.get("bbox"), dict) else None,
        }
        blocks_by_page.setdefault(page, []).append(block)

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
    for page_block in _page_blocks_for_review(job, page_numbers):
        for block in page_block.get("blocks", []):
            if not isinstance(block, dict):
                continue
            text = _normalize_text(block.get("text"))
            if len(text) < 6:
                continue
            signals = suspicious_text_signals(text)
            if not signals:
                continue
            suspicious_blocks.append({
                "page": block.get("page"),
                "review_id": block.get("review_id"),
                "type": block.get("type"),
                "level": block.get("level"),
                "text": text,
                "bbox": block.get("bbox") if isinstance(block.get("bbox"), dict) else None,
                "signals": signals,
            })
            if len(suspicious_blocks) >= MAX_SUSPICIOUS_READING_BLOCKS:
                return suspicious_blocks
    return suspicious_blocks


def _table_targets_for_review(job: Job, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    structure_json = _parse_job_structure(job)
    elements = structure_json.get("elements")
    if not isinstance(elements, list):
        return []

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

    targets_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_element in enumerate(elements):
        if not isinstance(raw_element, dict):
            continue
        if raw_element.get("type") != "table":
            continue
        page_raw = raw_element.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) and page_raw >= 0 else None
        if page is None:
            continue
        review_id = str(raw_element.get("review_id") or f"review-{index}")
        if requested_ids and review_id not in requested_ids:
            continue
        if requested_pages and page not in requested_pages:
            continue

        cells = raw_element.get("cells")
        if not isinstance(cells, list) or not cells:
            continue

        normalized_cells: list[dict[str, Any]] = []
        header_rows: set[int] = set()
        row_header_columns: set[int] = set()
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row = int(cell.get("row", 0)) if isinstance(cell.get("row"), int) else 0
            col = int(cell.get("col", 0)) if isinstance(cell.get("col"), int) else 0
            row_span = int(cell.get("row_span", 1)) if isinstance(cell.get("row_span"), int) else 1
            col_span = int(cell.get("col_span", 1)) if isinstance(cell.get("col_span"), int) else 1
            column_header = bool(cell.get("column_header", False))
            row_header = bool(cell.get("row_header", False))
            if column_header:
                header_rows.add(row)
            if row_header:
                row_header_columns.add(col)
            normalized_cells.append({
                "row": row,
                "col": col,
                "row_span": row_span,
                "col_span": col_span,
                "text": _normalize_text(cell.get("text"))[:120],
                "column_header": column_header,
                "row_header": row_header,
                "is_header": bool(cell.get("is_header", False) or column_header or row_header),
            })

        if not normalized_cells:
            continue

        targets_by_id[review_id] = {
            "table_review_id": review_id,
            "page": page,
            "bbox": raw_element.get("bbox") if isinstance(raw_element.get("bbox"), dict) else None,
            "num_rows": raw_element.get("num_rows", 0),
            "num_cols": raw_element.get("num_cols", 0),
            "header_rows": sorted(header_rows),
            "row_header_columns": sorted(row_header_columns),
            "cells": normalized_cells,
            "text_excerpt": _normalize_text(raw_element.get("text"))[:240],
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

    pdf_path = _job_pdf_path(job)
    raw_targets = metadata.get("font_review_targets")
    enriched_targets = _enrich_font_review_targets(
        pdf_path,
        raw_targets if isinstance(raw_targets, list) else [],
    )
    images = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": _render_page_image(pdf_path, page_number)},
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


def _reading_order_task_payload(job: Job, task: ReviewTask) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    structure_fragments = _collect_structure_fragments(job)
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

    pdf_path = _job_pdf_path(job)
    images = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": _render_page_image(pdf_path, page_number)},
        })

    suspicious_blocks = _suspicious_reading_blocks(job, page_numbers)
    suspicious_block_previews: list[dict[str, Any]] = []
    for block in suspicious_blocks:
        bbox = block.get("bbox")
        page = block.get("page")
        if not isinstance(page, int) or not isinstance(bbox, dict):
            continue
        try:
            preview_url = render_bbox_preview_png_data_url(pdf_path, page, bbox)
        except Exception:
            continue
        suspicious_block_previews.append({
            "page": page,
            "review_id": block.get("review_id"),
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
        "accessibility_goal": (
            "Use the page image and nearby structure to infer what a screen reader user "
            "should hear when extracted text looks garbled or oddly spaced."
        ),
        "reading_order_metrics": metadata,
        "pages_to_check": page_numbers,
        "structure_fragments": structure_fragments,
        "page_blocks": _page_blocks_for_review(job, page_numbers),
        "page_structure_fragments": _page_structure_fragments(job, page_numbers),
        "suspicious_text_blocks": suspicious_blocks,
        "suspicious_block_previews": suspicious_block_previews,
    }
    prompt_text = (
        f"{READING_ORDER_PROMPT}\n\n"
        "Image order: full-page previews first, then suspicious text block previews in the same order as suspicious_block_previews.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


def _table_target_payload(
    job: Job,
    task: ReviewTask,
    target: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    page = target.get("page")
    page_numbers = [int(page)] if isinstance(page, int) and page > 0 else [1]

    pdf_path = _job_pdf_path(job)
    images: list[dict[str, Any]] = []
    images.append({
        "type": "image_url",
        "image_url": {"url": _render_page_image(pdf_path, page_numbers[0])},
    })

    bbox = target.get("bbox")
    target_preview: dict[str, Any] | None = None
    if isinstance(page, int) and isinstance(bbox, dict):
        try:
            preview_url = render_bbox_preview_png_data_url(pdf_path, page, bbox)
        except Exception:
            preview_url = None
        if preview_url:
            target_preview = {
                "table_review_id": target.get("table_review_id"),
                "page": page,
                "risk_reasons": target.get("risk_reasons", []),
            }
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
        "accessibility_goal": (
            "Identify header rows and row-header columns only when they improve how "
            "screen readers and other assistive technologies understand this table."
        ),
        "page_to_check": page_numbers[0],
        "table_review_target": target,
        "target_preview": target_preview,
        "page_structure_fragments": _page_structure_fragments(job, page_numbers),
        "table_metrics": metadata,
    }
    prompt_text = (
        f"{TABLE_REVIEW_PROMPT}\n\n"
        "Image order: full-page preview first, then the crop preview for this one table if available.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


def _table_task_payload(job: Job, task: ReviewTask) -> tuple[str, list[dict[str, Any]]]:
    metadata = _parse_metadata(task)
    table_targets = _table_targets_for_review(job, metadata)
    page_numbers = sorted({
        int(target["page"])
        for target in table_targets
        if isinstance(target.get("page"), int) and int(target["page"]) > 0
    })[:MAX_REVIEW_PAGES]
    if not page_numbers:
        page_numbers = [
            page
            for page in metadata.get("pages_to_check", [])
            if isinstance(page, int) and page > 0
        ][:MAX_REVIEW_PAGES]
    if not page_numbers:
        page_numbers = [1]

    pdf_path = _job_pdf_path(job)
    images: list[dict[str, Any]] = []
    for page_number in page_numbers:
        images.append({
            "type": "image_url",
            "image_url": {"url": _render_page_image(pdf_path, page_number)},
        })

    target_previews: list[dict[str, Any]] = []
    for target in table_targets:
        bbox = target.get("bbox")
        page = target.get("page")
        if not isinstance(page, int) or not isinstance(bbox, dict):
            continue
        try:
            preview_url = render_bbox_preview_png_data_url(pdf_path, page, bbox)
        except Exception:
            continue
        target_previews.append({
            "table_review_id": target.get("table_review_id"),
            "page": page,
            "risk_reasons": target.get("risk_reasons", []),
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
        "pages_to_check": page_numbers,
        "table_review_targets": table_targets,
        "target_previews": target_previews,
        "page_structure_fragments": _page_structure_fragments(job, page_numbers),
        "table_metrics": metadata,
    }
    prompt_text = (
        f"{TABLE_REVIEW_PROMPT}\n\n"
        "Image order: full-page previews first, then table crop previews in the same order as target_previews.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=True)}"
    )
    content = [{"type": "text", "text": prompt_text}, *images]
    return prompt_text, content


async def _request_llm_json(
    *,
    llm_client: LlmClient,
    content: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": content}],
            temperature=0,
        )

    try:
        message_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected LLM response format: {exc}") from exc
    return _extract_json_object(str(message_content))


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


async def generate_review_suggestion(
    *,
    job: Job,
    task: ReviewTask,
    llm_client: LlmClient,
) -> dict[str, Any]:
    if task.task_type not in SUPPORTED_SUGGESTION_TASK_TYPES:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")

    if task.task_type == "font_text_fidelity":
        _prompt_text, content = _font_task_payload(job, task)
        suggestion = await _request_llm_json(llm_client=llm_client, content=content)
    elif task.task_type == "reading_order":
        _prompt_text, content = _reading_order_task_payload(job, task)
        suggestion = await _request_llm_json(llm_client=llm_client, content=content)
    elif task.task_type == "table_semantics":
        metadata = _parse_metadata(task)
        table_targets = _table_targets_for_review(job, metadata)
        if not table_targets:
            _prompt_text, content = _table_task_payload(job, task)
            suggestion = await _request_llm_json(llm_client=llm_client, content=content)
        else:
            per_table_suggestions: list[dict[str, Any]] = []
            for target in table_targets:
                _prompt_text, content = _table_target_payload(job, task, target)
                per_table_suggestions.append(
                    await _request_llm_json(llm_client=llm_client, content=content)
                )
            suggestion = _aggregate_table_suggestions(
                per_table_suggestions,
                total_targets=len(table_targets),
            )
    else:
        raise ValueError(f"Suggestions are not supported for task type '{task.task_type}'")
    suggestion.setdefault("task_type", task.task_type)
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

    pdf_path = _job_pdf_path(job)
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
