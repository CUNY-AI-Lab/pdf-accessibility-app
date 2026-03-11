from app.services.document_intelligence import build_document_model
from app.services.intelligence_merge import (
    apply_reading_order_overlay,
    apply_table_intelligence,
    apply_table_overlay,
    document_overlay_for_intelligence,
)


def _document():
    return build_document_model(
        structure_json={
            "elements": [
                {"review_id": "review-1", "type": "paragraph", "page": 0, "text": "First block"},
                {"review_id": "review-2", "type": "paragraph", "page": 0, "text": "Second block"},
                {
                    "review_id": "review-3",
                    "type": "table",
                    "page": 0,
                    "text": "Sample table",
                    "num_rows": 2,
                    "num_cols": 2,
                    "cells": [
                        {"row": 0, "col": 0, "text": "A"},
                        {"row": 0, "col": 1, "text": "B"},
                        {"row": 1, "col": 0, "text": "C"},
                        {"row": 1, "col": 1, "text": "D"},
                    ],
                },
            ]
        }
    )


def test_apply_reading_order_overlay_reorders_and_relabels():
    document, affected_pages = apply_reading_order_overlay(
        _document(),
        {
            "task_type": "reading_order",
            "confidence": "high",
            "proposed_page_orders": [
                {"page": 1, "ordered_review_ids": ["review-2", "review-1"]},
            ],
            "proposed_element_updates": [
                {"page": 1, "review_id": "review-1", "new_type": "artifact"},
            ],
        },
    )

    assert affected_pages == [1]
    page = document.pages[0]
    assert [block.review_id for block in page.blocks] == ["review-2", "review-1"]
    assert page.blocks[1].role == "artifact"
    assert page.blocks[0].provenance == "gemini_remediation_intelligence"


def test_apply_table_overlay_updates_header_flags():
    document, affected_pages = apply_table_overlay(
        _document(),
        {
            "task_type": "table_semantics",
            "confidence": "medium",
            "proposed_table_updates": [
                {
                    "page": 1,
                    "table_review_id": "review-3",
                    "header_rows": [0],
                    "row_header_columns": [0],
                }
            ],
        },
    )

    assert affected_pages == [1]
    table = document.pages[0].tables[0]
    assert table.header_rows == [0]
    assert table.row_header_columns == [0]
    assert table.cells[0].column_header is True
    assert table.cells[2].row_header is True
    assert table.provenance == "gemini_remediation_intelligence"


def test_apply_table_intelligence_updates_headers_and_tracks_provenance():
    document, affected_pages = apply_table_intelligence(
        _document(),
        [
            {
                "task_type": "table_intelligence",
                "confidence_score": 0.9,
                "suggested_action": "set_table_headers",
                "table_review_id": "review-3",
                "page": 1,
                "header_rows": [0],
                "row_header_columns": [0],
            }
        ],
    )

    assert affected_pages == [1]
    table = document.pages[0].tables[0]
    assert table.header_rows == [0]
    assert table.row_header_columns == [0]
    assert table.provenance == "gemini_remediation_intelligence"


def test_document_overlay_for_intelligence_returns_affected_pages_only():
    overlay = document_overlay_for_intelligence(
        _document(),
        {
            "task_type": "reading_order",
            "confidence": "high",
            "proposed_page_orders": [
                {"page": 1, "ordered_review_ids": ["review-2", "review-1"]},
            ],
            "proposed_element_updates": [],
        },
    )

    assert overlay["provenance"] == "gemini_remediation_intelligence"
    assert len(overlay["pages"]) == 1
    assert overlay["pages"][0]["page_number"] == 1


def test_document_overlay_for_intelligence_uses_direct_table_intelligence():
    overlay = document_overlay_for_intelligence(
        _document(),
        {
            "task_type": "table_semantics",
            "table_intelligence": [
                {
                    "task_type": "table_intelligence",
                    "confidence_score": 0.9,
                    "suggested_action": "manual_only",
                    "table_review_id": "review-3",
                    "page": 1,
                    "header_rows": [],
                    "row_header_columns": [],
                }
            ],
        },
    )

    assert overlay["provenance"] == "gemini_remediation_intelligence"
    assert len(overlay["pages"]) == 1
    assert overlay["pages"][0]["page_number"] == 1


def test_apply_reading_order_overlay_carries_semantic_text_hint():
    document, _affected_pages = apply_reading_order_overlay(
        _document(),
        {
            "task_type": "reading_order",
            "confidence": "high",
            "proposed_page_orders": [],
            "proposed_element_updates": [],
            "readable_text_hints": [
                {
                    "page": 1,
                    "review_id": "review-1",
                    "extracted_text": "F i r s t  b l o c k",
                    "native_text_candidate": "F i r s t  b l o c k",
                    "ocr_text_candidate": "First block",
                    "readable_text_hint": "First block, correctly spaced",
                    "chosen_source": "ocr",
                    "issue_type": "spacing_only",
                    "confidence": "high",
                    "should_block_accessibility": True,
                    "reason": "OCR matches the visible title more closely than the native extraction.",
                }
            ],
        },
    )

    block = document.pages[0].blocks[0]
    assert block.semantic_text_hint == "First block, correctly spaced"
    assert block.llm_text_candidate == "First block, correctly spaced"
    assert block.resolved_text == "First block, correctly spaced"
    assert block.resolution_source == "ocr"
    assert block.resolution_reason == "OCR matches the visible title more closely than the native extraction."
    assert block.semantic_issue_type == "spacing_only"
    assert block.semantic_blocking is True
