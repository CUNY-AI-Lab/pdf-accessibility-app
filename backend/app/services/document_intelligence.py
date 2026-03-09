from __future__ import annotations

import json
from typing import Any

from app.models import Job
from app.services.document_intelligence_models import (
    BBoxModel,
    BlockModel,
    DocumentModel,
    PageModel,
    TableCellModel,
    TableModel,
)

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


def build_document_model(*, job: Job | None = None, structure_json: dict[str, Any] | None = None) -> DocumentModel:
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

    pages = [pages_by_number[page_number] for page_number in sorted(pages_by_number)]
    for page in pages:
        page.blocks.sort(key=lambda block: (block.order, block.review_id))
        page.tables.sort(key=lambda table: (table.order, table.table_review_id))

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
