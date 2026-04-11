from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.models import Job
from app.services.document_intelligence_models import (
    BBoxModel,
    BlockModel,
    DocumentModel,
    FieldModel,
    PageModel,
    TableCellModel,
    TableModel,
)
from app.services.form_fields import extract_widget_fields

logger = logging.getLogger(__name__)

LEGACY_PROVENANCE = "legacy_structure"


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _coerce_bbox(value: Any) -> BBoxModel | None:
    if not isinstance(value, dict):
        return None
    try:
        return BBoxModel(
            l=float(value["l"]),
            t=float(value["t"]),
            r=float(value["r"]),
            b=float(value["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _coerce_page(value: Any) -> int | None:
    if not isinstance(value, int) or value < 0:
        return None
    return value + 1


def _structure_dict(job: Job | None = None, structure_json: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(structure_json, dict):
        return structure_json
    if job is None or not job.structure_json:
        return {}
    try:
        parsed = json.loads(job.structure_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _existing_pdf_path(job: Job | None = None, pdf_path: Path | None = None) -> Path | None:
    if isinstance(pdf_path, Path) and pdf_path.exists():
        return pdf_path
    if job is None:
        return None
    for candidate in (getattr(job, "output_path", None), getattr(job, "input_path", None)):
        if not candidate:
            continue
        resolved = Path(str(candidate))
        if resolved.exists():
            return resolved
    return None


def build_document_model(
    *,
    job: Job | None = None,
    structure_json: dict[str, Any] | None = None,
    pdf_path: Path | None = None,
) -> DocumentModel:
    parsed = _structure_dict(job=job, structure_json=structure_json)
    title = _normalize_text(parsed.get("title"))
    elements = parsed.get("elements")
    if not isinstance(elements, list):
        return DocumentModel(title=title, provenance=LEGACY_PROVENANCE, pages=[])

    pages_by_number: dict[int, PageModel] = {}
    page_order_counts: dict[int, int] = {}

    for index, raw_element in enumerate(elements):
        if not isinstance(raw_element, dict):
            continue
        page_number = _coerce_page(raw_element.get("page"))
        if page_number is None:
            continue
        page = pages_by_number.setdefault(page_number, PageModel(page_number=page_number))
        page_order_counts[page_number] = page_order_counts.get(page_number, 0) + 1
        order = page_order_counts[page_number]
        review_id = str(raw_element.get("review_id") or f"review-{index}")
        role = str(raw_element.get("type") or "paragraph").strip() or "paragraph"
        text = _normalize_text(raw_element.get("text"))
        resolved_text = _normalize_text(
            raw_element.get("resolved_text")
            or raw_element.get("actual_text")
            or raw_element.get("semantic_text_hint")
        )
        bbox = _coerce_bbox(raw_element.get("bbox"))

        if role == "table":
            raw_cells = raw_element.get("cells")
            cells: list[TableCellModel] = []
            header_rows: set[int] = set()
            row_header_columns: set[int] = set()
            if isinstance(raw_cells, list):
                for raw_cell in raw_cells:
                    if not isinstance(raw_cell, dict):
                        continue
                    row = int(raw_cell.get("row", 0)) if isinstance(raw_cell.get("row"), int) else 0
                    col = int(raw_cell.get("col", 0)) if isinstance(raw_cell.get("col"), int) else 0
                    row_span = int(raw_cell.get("row_span", 1)) if isinstance(raw_cell.get("row_span"), int) else 1
                    col_span = int(raw_cell.get("col_span", 1)) if isinstance(raw_cell.get("col_span"), int) else 1
                    column_header = bool(raw_cell.get("column_header", False))
                    row_header = bool(raw_cell.get("row_header", False))
                    is_header = bool(raw_cell.get("is_header", False) or column_header or row_header)
                    if column_header:
                        header_rows.add(row)
                    if row_header:
                        row_header_columns.add(col)
                    cells.append(
                        TableCellModel(
                            row=row,
                            col=col,
                            text=_normalize_text(raw_cell.get("text"))[:120],
                            row_span=row_span,
                            col_span=col_span,
                            is_header=is_header,
                            column_header=column_header,
                            row_header=row_header,
                        )
                    )

            page.tables.append(
                TableModel(
                    table_review_id=review_id,
                    page=page_number,
                    order=order,
                    bbox=bbox,
                    num_rows=int(raw_element.get("num_rows", 0)) if isinstance(raw_element.get("num_rows"), int) else 0,
                    num_cols=int(raw_element.get("num_cols", 0)) if isinstance(raw_element.get("num_cols"), int) else 0,
                    text_excerpt=text[:240],
                    header_rows=sorted(header_rows),
                    row_header_columns=sorted(row_header_columns),
                    cells=cells,
                    provenance=LEGACY_PROVENANCE,
                    confidence=0.5,
                    source_ids=[review_id],
                )
            )
            continue

        level = raw_element.get("level") if isinstance(raw_element.get("level"), int) else None
        page.blocks.append(
            BlockModel(
                review_id=review_id,
                page=page_number,
                order=order,
                role=role,
                text=text[:240],
                native_text_candidate=text[:240],
                resolved_text=resolved_text[:240] or None,
                resolution_source=(
                    str(raw_element.get("resolution_source") or "").strip() or None
                ),
                resolution_reason=(
                    str(raw_element.get("resolution_reason") or "").strip() or None
                ),
                level=level,
                bbox=bbox,
                semantic_text_hint=resolved_text[:240] or None,
                semantic_issue_type=(
                    str(raw_element.get("semantic_issue_type") or "").strip() or None
                ),
                semantic_blocking=bool(raw_element.get("semantic_blocking", False)),
                provenance=LEGACY_PROVENANCE,
                confidence=0.5,
                source_ids=[review_id],
            )
        )

    existing_pdf = _existing_pdf_path(job=job, pdf_path=pdf_path)
    if existing_pdf is not None:
        try:
            for raw_field in extract_widget_fields(existing_pdf):
                page_number = int(raw_field["page"])
                page = pages_by_number.setdefault(page_number, PageModel(page_number=page_number))
                page.fields.append(
                    FieldModel(
                        field_review_id=str(raw_field["field_review_id"]),
                        page=page_number,
                        order=int(raw_field["order"]),
                        field_type=str(raw_field["field_type"]),
                        field_name=_normalize_text(raw_field.get("field_name"))[:240],
                        accessible_name=_normalize_text(raw_field.get("accessible_name"))[:240],
                        value_text=_normalize_text(raw_field.get("value_text"))[:240],
                        bbox=_coerce_bbox(raw_field.get("bbox")),
                        provenance="pdf_widgets",
                        confidence=0.6,
                        label_quality=str(raw_field.get("label_quality") or "missing"),
                        source_ids=[
                            value
                            for value in (
                                str(raw_field.get("widget_objgen") or "").strip(),
                                str(raw_field.get("field_objgen") or "").strip(),
                            )
                            if value
                        ],
                    )
                )
        except Exception:
            logger.warning("Failed to extract widget fields for document intelligence", exc_info=True)

    pages = [pages_by_number[page_number] for page_number in sorted(pages_by_number)]
    for page in pages:
        page.blocks.sort(key=lambda block: (block.order, block.review_id))
        page.tables.sort(key=lambda table: (table.order, table.table_review_id))
        page.fields.sort(key=lambda field: (field.order, field.field_review_id))

    return DocumentModel(
        title=title,
        pages=pages,
        provenance=LEGACY_PROVENANCE,
    )


def collect_structure_fragments(document: DocumentModel, *, max_fragments: int) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for page in document.pages:
        for block in page.blocks:
            if len(block.text) < 16:
                continue
            key = (page.page_number, block.role, block.text[:120])
            if key in seen:
                continue
            seen.add(key)
            fragments.append(
                {
                    "page": page.page_number,
                    "type": block.role,
                    "text": block.text[:240],
                    "bbox": block.bbox.to_dict() if block.bbox else None,
                    "review_id": block.review_id,
                    "provenance": block.provenance,
                    "confidence": block.confidence,
                }
            )
            if len(fragments) >= max_fragments:
                return fragments
    return fragments


def collect_nearby_blocks(
    document: DocumentModel,
    *,
    page_number: int,
    bbox: dict[str, Any] | None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    page = document.page(page_number)
    if page is None:
        return []
    if not isinstance(bbox, dict):
        return [
            {
                "review_id": block.review_id,
                "type": block.role,
                "text": block.text[:240],
                "bbox": block.bbox.to_dict() if block.bbox else None,
            }
            for block in page.blocks[:limit]
            if block.text
        ]

    try:
        center_x = (float(bbox["l"]) + float(bbox["r"])) / 2.0
        center_y = (float(bbox["t"]) + float(bbox["b"])) / 2.0
    except Exception:
        center_x = center_y = 0.0

    ranked: list[tuple[float, BlockModel]] = []
    for block in page.blocks:
        if not block.text or block.bbox is None:
            continue
        block_center_x = (block.bbox.l + block.bbox.r) / 2.0
        block_center_y = (block.bbox.t + block.bbox.b) / 2.0
        distance = abs(center_x - block_center_x) + abs(center_y - block_center_y)
        ranked.append((distance, block))

    ranked.sort(key=lambda item: (item[0], item[1].order, item[1].review_id))
    return [
        {
            "review_id": block.review_id,
            "type": block.role,
            "text": block.text[:240],
            "bbox": block.bbox.to_dict() if block.bbox else None,
        }
        for _, block in ranked[:limit]
    ]


def collect_enclosing_context_blocks(
    document: DocumentModel,
    *,
    page_number: int,
    bbox: dict[str, Any] | None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    page = document.page(page_number)
    if page is None or not isinstance(bbox, dict):
        return []
    try:
        field_left = float(bbox["l"])
        field_top = float(bbox["t"])
        field_right = float(bbox["r"])
    except Exception:
        return []

    ranked: list[tuple[float, float, int, BlockModel]] = []
    for block in page.blocks:
        if not block.text or block.bbox is None:
            continue
        # Prefer blocks that visually span the field and sit above it; these
        # are the strongest generic candidates for section/group context.
        if block.bbox.b < field_top - 4.0:
            continue
        vertical_gap = max(0.0, block.bbox.b - field_top)
        if vertical_gap > 180.0:
            continue
        block_width = max(0.0, block.bbox.r - block.bbox.l)
        field_width = max(1.0, field_right - field_left)
        broad_context = block_width >= max(field_width * 3.0, 120.0)
        if not broad_context:
            continue
        ranked.append((
            vertical_gap,
            -block_width,
            block.order,
            block,
        ))

    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3].review_id))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, _, _, block in ranked:
        text = block.text[:240]
        if not text or text in seen:
            continue
        results.append(
            {
                "review_id": block.review_id,
                "type": block.role,
                "text": text,
                "bbox": block.bbox.to_dict() if block.bbox else None,
                "context_role": "enclosing_or_group_context",
            }
        )
        seen.add(text)
        if len(results) >= limit:
            break
    return results
