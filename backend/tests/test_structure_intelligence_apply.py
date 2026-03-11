from app.services.structure_intelligence_apply import (
    applicable_actualtext_candidates,
    apply_reading_order_change,
    apply_table_change,
    can_accept_reading_order_change,
    can_accept_table_change,
)


def test_apply_reading_order_change_reorders_and_retypes():
    structure = {
        "elements": [
            {"page": 0, "type": "paragraph", "text": "first", "review_id": "review-0"},
            {"page": 0, "type": "paragraph", "text": "second", "review_id": "review-1"},
        ]
    }
    suggestion = {
        "proposed_page_orders": [{"page": 1, "ordered_review_ids": ["review-1", "review-0"]}],
        "proposed_element_updates": [
            {"review_id": "review-0", "new_type": "heading", "new_level": 2}
        ],
    }

    applied = apply_reading_order_change(structure, suggestion)

    assert applied is not None
    assert [element["text"] for element in applied["elements"]] == ["second", "first"]
    assert applied["elements"][1]["type"] == "heading"
    assert applied["elements"][1]["level"] == 2
    assert "review_id" not in applied["elements"][0]


def test_apply_table_change_sets_header_flags():
    structure = {
        "elements": [
            {
                "page": 0,
                "type": "table",
                "review_id": "review-7",
                "cells": [
                    {"row": 0, "col": 0},
                    {"row": 1, "col": 0},
                    {"row": 1, "col": 1},
                ],
            }
        ]
    }
    suggestion = {
        "proposed_table_updates": [
            {
                "table_review_id": "review-7",
                "header_rows": [0],
                "row_header_columns": [0],
                "suggested_action": "set_table_headers",
            }
        ]
    }

    applied = apply_table_change(structure, suggestion)

    assert applied is not None
    cells = applied["elements"][0]["cells"]
    assert cells[0]["column_header"] is True
    assert cells[0]["row_header"] is True
    assert cells[2]["is_header"] is False


def test_can_accept_reading_order_change_allows_confirm_current_order():
    assert (
        can_accept_reading_order_change(
            {"elements": []},
            {"suggested_action": "confirm_current_order"},
        )
        is True
    )


def test_can_accept_table_change_allows_confirm_current_headers():
    assert (
        can_accept_table_change(
            {"elements": []},
            {"suggested_action": "confirm_current_headers"},
        )
        is True
    )


def test_applicable_actualtext_candidates_filters_to_flagged_targets():
    task_metadata = {
        "font_review_targets": [
            {"page": 2, "operator_index": 10, "font": "ABC"},
        ]
    }
    suggestion = {
        "actualtext_candidates": [
            {"page": 2, "operator_index": 10, "font": "ABC", "proposed_actualtext": "Hello"},
            {"page": 2, "operator_index": 11, "font": "ABC", "proposed_actualtext": "Skip"},
        ]
    }

    candidates = applicable_actualtext_candidates(suggestion, task_metadata)

    assert candidates == [
        {
            "page": 2,
            "operator_index": 10,
            "font": "ABC",
            "proposed_actualtext": "Hello",
            "confidence": None,
            "reason": None,
        }
    ]
