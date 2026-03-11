from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.document_intelligence_models import DocumentModel
from app.services.intelligence_gemini import (
    normalize_reading_order_suggestion,
    normalize_table_suggestion,
)

GEMINI_PROVENANCE = "gemini_review_suggestion"


def _update_block_metadata(block, *, confidence: float) -> None:
    block.provenance = GEMINI_PROVENANCE
    block.confidence = max(float(block.confidence), float(confidence))
    if GEMINI_PROVENANCE not in block.source_ids:
        block.source_ids.append(GEMINI_PROVENANCE)


def _apply_block_resolution(block, hint: dict[str, Any], *, confidence: float) -> None:
    hint_text = str(hint.get("readable_text_hint") or "").strip()
    if hint_text:
        block.semantic_text_hint = hint_text
        block.llm_text_candidate = hint_text
        block.resolved_text = hint_text
    issue_type = str(hint.get("issue_type") or "").strip()
    if issue_type:
        block.semantic_issue_type = issue_type
    resolved_kind = str(hint.get("resolved_kind") or "").strip()
    if resolved_kind:
        block.semantic_resolved_kind = resolved_kind
    chosen_source = str(hint.get("chosen_source") or "llm_inferred").strip() or "llm_inferred"
    block.resolution_source = chosen_source
    reason = str(hint.get("reason") or "").strip()
    if reason:
        block.resolution_reason = reason
    block.semantic_blocking = bool(hint.get("should_block_accessibility", False))
    _update_block_metadata(block, confidence=confidence)


def _update_table_metadata(table, *, confidence: float) -> None:
    table.provenance = GEMINI_PROVENANCE
    table.confidence = max(float(table.confidence), float(confidence))
    if GEMINI_PROVENANCE not in table.source_ids:
        table.source_ids.append(GEMINI_PROVENANCE)


def apply_reading_order_overlay(document: DocumentModel, suggestion: dict[str, Any]) -> tuple[DocumentModel, list[int]]:
    normalized = normalize_reading_order_suggestion(suggestion)
    result = deepcopy(document)
    affected_pages: set[int] = set()
    confidence = float(normalized["confidence"])

    for page_update in normalized["proposed_page_orders"]:
        page = result.page(int(page_update["page"]))
        if page is None:
            continue
        blocks_by_id = {block.review_id: block for block in page.blocks}
        ordered_ids = list(page_update["ordered_review_ids"])
        if set(ordered_ids) != {block.review_id for block in page.blocks}:
            continue
        page.blocks = [blocks_by_id[review_id] for review_id in ordered_ids]
        for index, block in enumerate(page.blocks, start=1):
            block.order = index
            _update_block_metadata(block, confidence=confidence)
        affected_pages.add(page.page_number)

    for element_update in normalized["proposed_element_updates"]:
        page = result.page(int(element_update["page"]))
        if page is None:
            continue
        for block in page.blocks:
            if block.review_id != str(element_update["review_id"]):
                continue
            block.role = str(element_update["new_type"])
            new_level = element_update.get("new_level")
            block.level = int(new_level) if isinstance(new_level, int) else None
            _update_block_metadata(block, confidence=confidence)
            affected_pages.add(page.page_number)
            break

    for hint in normalized.get("readable_text_hints", []):
        page = result.page(int(hint["page"]))
        if page is None:
            continue
        for block in page.blocks:
            if block.review_id != str(hint["review_id"]):
                continue
            _apply_block_resolution(block, hint, confidence=confidence)
            affected_pages.add(page.page_number)
            break

    return result, sorted(affected_pages)


def apply_suspicious_text_intelligence(document: DocumentModel, intelligence: dict[str, Any]) -> tuple[DocumentModel, list[int]]:
    result = deepcopy(document)
    affected_pages: set[int] = set()
    confidence = float(intelligence.get("confidence_score", 0.5) or 0.5)

    for hint in intelligence.get("blocks", []):
        if not isinstance(hint, dict):
            continue
        page = result.page(int(hint["page"]))
        if page is None:
            continue
        for block in page.blocks:
            if block.review_id != str(hint["review_id"]):
                continue
            _apply_block_resolution(block, hint, confidence=confidence)
            affected_pages.add(page.page_number)
            break

    return result, sorted(affected_pages)


def apply_table_overlay(document: DocumentModel, suggestion: dict[str, Any]) -> tuple[DocumentModel, list[int]]:
    normalized = normalize_table_suggestion(suggestion)
    result = deepcopy(document)
    affected_pages: set[int] = set()
    confidence = float(normalized["confidence"])

    for table_update in normalized["proposed_table_updates"]:
        page = result.page(int(table_update["page"]))
        if page is None:
            continue
        for table in page.tables:
            if table.table_review_id != str(table_update["table_review_id"]):
                continue
            header_rows = set(int(value) for value in table_update["header_rows"])
            row_header_columns = set(int(value) for value in table_update["row_header_columns"])
            table.header_rows = sorted(header_rows)
            table.row_header_columns = sorted(row_header_columns)
            for cell in table.cells:
                cell.column_header = cell.row in header_rows
                cell.row_header = cell.col in row_header_columns
                cell.is_header = cell.column_header or cell.row_header
            _update_table_metadata(table, confidence=confidence)
            affected_pages.add(page.page_number)
            break

    return result, sorted(affected_pages)


def apply_table_intelligence(document: DocumentModel, intelligence_items: list[dict[str, Any]]) -> tuple[DocumentModel, list[int]]:
    result = deepcopy(document)
    affected_pages: set[int] = set()

    for item in intelligence_items:
        if not isinstance(item, dict):
            continue
        page_number = item.get("page")
        table_review_id = str(item.get("table_review_id") or "").strip()
        if not isinstance(page_number, int) or page_number < 1 or not table_review_id:
            continue
        page = result.page(page_number)
        if page is None:
            continue
        confidence = float(item.get("confidence_score", 0.5) or 0.5)
        header_rows = set(
            int(value)
            for value in item.get("header_rows", [])
            if isinstance(value, int) and value >= 0
        )
        row_header_columns = set(
            int(value)
            for value in item.get("row_header_columns", [])
            if isinstance(value, int) and value >= 0
        )
        for table in page.tables:
            if table.table_review_id != table_review_id:
                continue
            if str(item.get("suggested_action") or "").strip() == "set_table_headers":
                table.header_rows = sorted(header_rows)
                table.row_header_columns = sorted(row_header_columns)
                for cell in table.cells:
                    cell.column_header = cell.row in header_rows
                    cell.row_header = cell.col in row_header_columns
                    cell.is_header = cell.column_header or cell.row_header
            _update_table_metadata(table, confidence=confidence)
            affected_pages.add(page.page_number)
            break

    return result, sorted(affected_pages)


def document_overlay_for_suggestion(document: DocumentModel, suggestion: dict[str, Any]) -> dict[str, Any]:
    task_type = str(suggestion.get("task_type") or "").strip()
    if task_type == "reading_order":
        overlaid, affected_pages = apply_reading_order_overlay(document, suggestion)
    elif task_type == "table_semantics":
        if isinstance(suggestion.get("table_intelligence"), list):
            overlaid, affected_pages = apply_table_intelligence(document, suggestion["table_intelligence"])
        else:
            overlaid, affected_pages = apply_table_overlay(document, suggestion)
    else:
        return {"provenance": GEMINI_PROVENANCE, "pages": []}

    return {
        "provenance": GEMINI_PROVENANCE,
        "pages": [
            page.to_dict()
            for page in overlaid.pages
            if page.page_number in affected_pages
        ],
    }
