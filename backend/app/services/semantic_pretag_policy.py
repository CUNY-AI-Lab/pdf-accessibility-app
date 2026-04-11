"""Table and form pre-tag semantic policy helpers."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from app.services.document_intelligence import (
    build_document_model,
    collect_enclosing_context_blocks,
    collect_nearby_blocks,
    collect_structure_fragments,
)
from app.services.form_fields import field_label_quality

PRETAG_TABLE_ALLOWED_ACTIONS = frozenset({"confirm_current_headers", "set_table_headers"})
PRETAG_FORM_ALLOWED_TYPES = frozenset({
    "text",
    "checkbox",
    "radio_button",
    "push_button",
    "combo_box",
    "list_box",
})
PRETAG_WIDGET_RATIONALIZATION_ALLOWED_TYPES = frozenset({"text"})
PAGE_CHROME_RE = re.compile(r"^\d+\s*\|?\s*p\s*a\s*g\s*e$", re.IGNORECASE)


def _normalize_widget_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _looks_like_page_chrome(value: str) -> bool:
    text = _normalize_widget_text(value)
    if not text:
        return False
    return bool(PAGE_CHROME_RE.match(text))


def suspicious_widget_candidates(fields: list[dict[str, object]]) -> list[dict[str, object]]:
    # Disable heuristic auto-rationalization of widgets. Static text widgets now
    # require stronger document evidence or manual review instead of rule-based suspicion.
    return []


def table_page_structure_fragments(
    structure_json: dict[str, object],
    *,
    page_numbers: list[int],
    max_fragments: int = 10,
) -> list[dict[str, object]]:
    document = build_document_model(structure_json=structure_json)
    requested_pages = {page for page in page_numbers if isinstance(page, int) and page > 0}
    fragments: list[dict[str, object]] = []
    for fragment in collect_structure_fragments(document, max_fragments=max_fragments * 2):
        page = fragment.get("page")
        if isinstance(page, int) and page in requested_pages:
            fragments.append(fragment)
        if len(fragments) >= max_fragments:
            break
    return fragments


def table_targets_with_cells(
    structure_json: dict[str, object],
    requested_ids: set[str],
) -> dict[str, dict[str, object]]:
    document = build_document_model(structure_json=structure_json)
    targets: dict[str, dict[str, object]] = {}
    for page in document.pages:
        for table in page.tables:
            review_id = str(table.table_review_id or "").strip()
            if not review_id or (requested_ids and review_id not in requested_ids):
                continue
            targets[review_id] = {
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
    return targets


def should_auto_apply_table_intelligence(item: dict[str, object]) -> bool:
    if str(item.get("confidence") or "").strip() != "high":
        return False
    action = str(item.get("suggested_action") or "").strip()
    return action in PRETAG_TABLE_ALLOWED_ACTIONS


def apply_table_intelligence_to_element(
    element: dict[str, object],
    *,
    action: str,
    header_rows: list[int],
    row_header_columns: list[int],
) -> bool:
    cells = element.get("cells")
    if not isinstance(cells, list) or not cells:
        return False

    header_row_set = {int(value) for value in header_rows if isinstance(value, int) and value >= 0}
    row_header_col_set = {int(value) for value in row_header_columns if isinstance(value, int) and value >= 0}

    updated = False
    if action == "set_table_headers":
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row = cell.get("row")
            col = cell.get("col")
            column_header = isinstance(row, int) and row in header_row_set
            row_header = isinstance(col, int) and col in row_header_col_set
            if cell.get("column_header") != column_header:
                cell["column_header"] = column_header
                updated = True
            if cell.get("row_header") != row_header:
                cell["row_header"] = row_header
                updated = True
            is_header = column_header or row_header
            if cell.get("is_header") != is_header:
                cell["is_header"] = is_header
                updated = True
    return updated or action == "confirm_current_headers"


def should_retry_table_intelligence_aggressively(
    target: dict[str, object],
    item: dict[str, object],
) -> bool:
    if not isinstance(target, dict) or not isinstance(item, dict):
        return False
    if str(item.get("suggested_action") or "").strip() in PRETAG_TABLE_ALLOWED_ACTIONS:
        return False
    reasons = {
        str(reason).strip()
        for reason in target.get("risk_reasons", [])
        if str(reason).strip()
    } if isinstance(target.get("risk_reasons"), list) else set()
    if not reasons:
        return False
    allowed = {
        "merged cells or spans present",
        "large table matrix",
        "very dense table",
        "multi-level header pattern",
    }
    return reasons.issubset(allowed)


def should_retry_table_intelligence_confirm_existing(
    target: dict[str, object],
    item: dict[str, object],
) -> bool:
    if not isinstance(target, dict) or not isinstance(item, dict):
        return False
    if str(item.get("suggested_action") or "").strip() in PRETAG_TABLE_ALLOWED_ACTIONS:
        return False
    header_rows = target.get("header_rows")
    row_header_columns = target.get("row_header_columns")
    has_existing_headers = bool(
        (isinstance(header_rows, list) and header_rows)
        or (isinstance(row_header_columns, list) and row_header_columns)
    )
    if not has_existing_headers:
        return False
    reasons = {
        str(reason).strip()
        for reason in target.get("risk_reasons", [])
        if str(reason).strip()
    } if isinstance(target.get("risk_reasons"), list) else set()
    if not reasons:
        return False
    allowed = {
        "merged cells or spans present",
        "large table matrix",
        "very dense table",
        "multi-level header pattern",
    }
    return reasons.issubset(allowed)


def form_targets_for_intelligence(
    *,
    working_pdf: Path,
    structure_json: dict[str, object],
) -> list[dict[str, object]]:
    def _bbox_distance(a: dict[str, object] | None, b: dict[str, object] | None) -> float:
        if not isinstance(a, dict) or not isinstance(b, dict):
            return float("inf")
        try:
            ax = (float(a["l"]) + float(a["r"])) / 2.0
            ay = (float(a["t"]) + float(a["b"])) / 2.0
            bx = (float(b["l"]) + float(b["r"])) / 2.0
            by = (float(b["t"]) + float(b["b"])) / 2.0
        except Exception:
            return float("inf")
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    document = build_document_model(structure_json=structure_json, pdf_path=working_pdf)
    targets: list[dict[str, object]] = []
    for page in document.pages:
        for field in page.fields:
            if field.field_type not in PRETAG_FORM_ALLOWED_TYPES:
                continue
            if field.label_quality not in {"missing", "weak"}:
                continue
            field_bbox = field.bbox.to_dict() if field.bbox else None
            nearby_fields: list[dict[str, object]] = []
            for other in page.fields:
                if other.field_review_id == field.field_review_id:
                    continue
                other_bbox = other.bbox.to_dict() if other.bbox else None
                close = abs(other.order - field.order) <= 3
                if field_bbox and other_bbox:
                    overlaps_vertically = not (
                        float(other_bbox["t"]) < float(field_bbox["b"]) or float(other_bbox["b"]) > float(field_bbox["t"])
                    )
                    overlaps_horizontally = not (
                        float(other_bbox["r"]) < float(field_bbox["l"]) or float(other_bbox["l"]) > float(field_bbox["r"])
                    )
                    vertical_gap = min(
                        abs(float(field_bbox["t"]) - float(other_bbox["b"])),
                        abs(float(other_bbox["t"]) - float(field_bbox["b"])),
                    )
                    horizontal_gap = min(
                        abs(float(field_bbox["l"]) - float(other_bbox["r"])),
                        abs(float(other_bbox["l"]) - float(field_bbox["r"])),
                    )
                    close = (
                        close
                        or overlaps_vertically
                        or overlaps_horizontally
                        or vertical_gap <= 24.0
                        or horizontal_gap <= 48.0
                    )
                if not close:
                    continue
                nearby_fields.append(
                    {
                        "field_review_id": other.field_review_id,
                        "field_type": other.field_type,
                        "field_name": other.field_name,
                        "accessible_name": other.accessible_name,
                        "label_quality": other.label_quality,
                        "bbox": other_bbox,
                        "_distance": _bbox_distance(field_bbox, other_bbox),
                        "_order_gap": abs(other.order - field.order),
                    }
                )
            nearby_fields.sort(
                key=lambda item: (
                    float(item.get("_distance", float("inf"))),
                    int(item.get("_order_gap", 9999)),
                    str(item.get("field_review_id", "")),
                )
            )
            targets.append(
                {
                    "field_review_id": field.field_review_id,
                    "page": page.page_number,
                    "field_type": field.field_type,
                    "field_name": field.field_name,
                    "accessible_name": field.accessible_name,
                    "label_quality": field.label_quality,
                    "bbox": field_bbox,
                    "value_text": field.value_text[:120],
                    "nearby_blocks": collect_nearby_blocks(
                        document,
                        page_number=page.page_number,
                        bbox=field_bbox,
                        limit=6,
                    ),
                    "context_blocks": collect_enclosing_context_blocks(
                        document,
                        page_number=page.page_number,
                        bbox=field_bbox,
                        limit=4,
                    ),
                    "nearby_fields": [
                        {
                            key: value
                            for key, value in nearby_field.items()
                            if not str(key).startswith("_")
                        }
                        for nearby_field in nearby_fields[:6]
                    ],
                }
            )
    return targets


def widget_targets_for_rationalization(
    *,
    working_pdf: Path,
    structure_json: dict[str, object],
) -> list[dict[str, object]]:
    document = build_document_model(structure_json=structure_json, pdf_path=working_pdf)
    raw_fields: list[dict[str, object]] = []
    page_figure_counts: Counter[int] = Counter()
    page_table_counts: Counter[int] = Counter()
    elements = structure_json.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if not isinstance(element, dict):
                continue
            page_raw = element.get("page")
            if not isinstance(page_raw, int) or page_raw < 0:
                continue
            page_number = page_raw + 1
            if element.get("type") == "figure":
                page_figure_counts[page_number] += 1
            elif element.get("type") == "table":
                page_table_counts[page_number] += 1

    for page in document.pages:
        for field in page.fields:
            raw_fields.append(
                {
                    "field_review_id": field.field_review_id,
                    "page": page.page_number,
                    "order": field.order,
                    "field_type": field.field_type,
                    "field_name": field.field_name,
                    "accessible_name": field.accessible_name,
                    "label_quality": field.label_quality,
                    "value_text": field.value_text,
                    "bbox": field.bbox.to_dict() if field.bbox else None,
                }
            )

    candidate_by_id = {
        str(candidate.get("field_review_id") or "").strip(): candidate
        for candidate in suspicious_widget_candidates(raw_fields)
        if str(candidate.get("field_review_id") or "").strip()
    }
    if not candidate_by_id:
        return []

    targets: list[dict[str, object]] = []
    for page in document.pages:
        for field in page.fields:
            review_id = str(field.field_review_id or "").strip()
            candidate = candidate_by_id.get(review_id)
            if not candidate:
                continue
            field_bbox = field.bbox.to_dict() if field.bbox else None
            page_figure_like_count = int(page_figure_counts.get(page.page_number, 0))
            page_table_count = int(page_table_counts.get(page.page_number, 0))
            suspicion_reasons = {
                str(reason).strip()
                for reason in candidate.get("suspicion_reasons", [])
                if str(reason).strip()
            }
            if (
                suspicion_reasons == {"natural_language_static_text"}
                and page_figure_like_count <= 0
                and page_table_count <= 0
            ):
                continue
            targets.append(
                {
                    **candidate,
                    "nearby_blocks": collect_nearby_blocks(
                        document,
                        page_number=page.page_number,
                        bbox=field_bbox,
                        limit=6,
                    ),
                    "page_figure_like_count": page_figure_like_count,
                    "page_table_count": page_table_count,
                }
            )
    return targets


def should_auto_apply_form_intelligence(item: dict[str, object]) -> bool:
    if str(item.get("confidence") or "").strip() != "high":
        return False
    if str(item.get("suggested_action") or "").strip() != "set_field_label":
        return False
    label = str(item.get("accessible_label") or "").strip()
    if not label or len(label) > 400:
        return False
    current_label = str(item.get("current_accessible_name") or "").strip()
    current_field_name = str(item.get("current_field_name") or "").strip()
    if field_label_quality(accessible_name=label, field_name=current_field_name) != "good":
        return False
    if current_label and label == current_label:
        return False
    return True


def should_auto_remove_widget(item: dict[str, object]) -> bool:
    if str(item.get("confidence") or "").strip() != "high":
        return False
    if str(item.get("suggested_action") or "").strip() != "remove_static_widget":
        return False
    return bool(str(item.get("field_review_id") or "").strip())
