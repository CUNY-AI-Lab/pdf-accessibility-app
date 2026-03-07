from app.services.toc_suggestions import apply_toc_llm_suggestion, collect_toc_candidates


def test_collect_toc_candidates_detects_heading_and_table_entries():
    structure_json = {
        "elements": [
            {"type": "heading", "text": "Contents", "page": 0},
            {"type": "table", "page": 0, "num_rows": 2, "num_cols": 3, "cells": [
                {"row": 0, "col": 0, "text": "1"},
                {"row": 0, "col": 1, "text": "Introduction"},
                {"row": 0, "col": 2, "text": "3"},
                {"row": 1, "col": 0, "text": "2"},
                {"row": 1, "col": 1, "text": "Installation"},
                {"row": 1, "col": 2, "text": "6"},
            ]},
            {"type": "heading", "text": "PDFlib GmbH ........ 7", "page": 0},
            {"type": "paragraph", "text": "Regular body text", "page": 1},
        ],
    }

    groups = collect_toc_candidates(structure_json)

    assert len(groups) == 1
    assert groups[0]["caption_index"] == 0
    assert [item["index"] for item in groups[0]["candidate_elements"][:2]] == [1, 2]
    assert groups[0]["candidate_elements"][0]["type"] == "table"
    assert groups[0]["candidate_elements"][1]["type"] == "heading"


def test_collect_toc_candidates_skips_when_toc_already_present():
    structure_json = {
        "elements": [
            {"type": "toc_caption", "text": "Contents", "page": 0},
            {"type": "toc_item", "text": "Introduction ........ 3", "page": 0},
        ],
    }

    assert collect_toc_candidates(structure_json) == []


def test_apply_toc_llm_suggestion_marks_caption_and_entries():
    structure_json = {
        "elements": [
            {"type": "heading", "text": "Contents", "page": 0},
            {"type": "table", "page": 0, "num_rows": 2, "num_cols": 3, "cells": [
                {"row": 0, "col": 0, "text": "1"},
                {"row": 0, "col": 1, "text": "Introduction"},
                {"row": 0, "col": 2, "text": "3"},
                {"row": 1, "col": 0, "text": "2"},
                {"row": 1, "col": 1, "text": "Installation"},
                {"row": 1, "col": 2, "text": "6"},
            ]},
            {"type": "heading", "text": "PDFlib GmbH ........ 7", "page": 0},
        ],
    }
    suggestion = {
        "groups": [
            {
                "caption_index": 0,
                "is_toc": True,
                "confidence": "high",
                "entry_indexes": [1, 2],
                "entry_types": {
                    "1": "toc_item_table",
                    "2": "toc_item",
                },
            }
        ]
    }

    audit = apply_toc_llm_suggestion(structure_json, suggestion)

    assert audit["applied"] is True
    assert structure_json["elements"][0]["type"] == "toc_caption"
    assert [element["type"] for element in structure_json["elements"]] == [
        "toc_caption",
        "toc_item",
        "toc_item",
        "toc_item",
    ]
    assert structure_json["elements"][1]["text"] == "1 Introduction 3"
    assert structure_json["elements"][2]["text"] == "2 Installation 6"
    assert structure_json["elements"][3]["text"] == "PDFlib GmbH ........ 7"
    assert structure_json["elements"][0]["toc_group_ref"] == structure_json["elements"][1]["toc_group_ref"]
