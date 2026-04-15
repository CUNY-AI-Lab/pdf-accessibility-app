from types import SimpleNamespace

import httpx
import pytest

from app.pipeline import structure
from app.pipeline.structure import (
    _docling_serve_request,
    _expand_toc_item_tables,
    _extract_bbox,
    _extract_title_from_docling,
    _mark_toc_sequences,
    _normalize_docling_elements,
    _normalize_lang_tag,
    _normalize_native_toc,
    _rebuild_toc_elements_from_page_rows,
    _toc_rows_from_word_cells,
)


@pytest.mark.asyncio
async def test_docling_serve_request_retries_transient_connect_timeout(monkeypatch):
    sleep_delays = []

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(structure.asyncio, "sleep", fake_sleep)

    class Client:
        attempts = 0

        async def request(self, method, url, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.ConnectTimeout("connect timed out")
            return SimpleNamespace(status_code=200)

    client = Client()

    response = await _docling_serve_request(
        client,
        "GET",
        "https://example.test/v1/status/poll/task",
        attempts=2,
        retry_delay=0.01,
    )

    assert response.status_code == 200
    assert client.attempts == 2
    assert sleep_delays == [0.01]


def test_normalize_docling_elements_maps_footnotes_to_note_elements():
    doc_dict = {
        "body": {
            "children": [{"$ref": "#/texts/0"}],
        },
        "texts": [
            {
                "label": "footnote",
                "text": "Footnote content",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 10, "b": 20, "r": 100, "t": 40},
                    }
                ],
            }
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert elements == [
        {
            "type": "note",
            "text": "Footnote content",
            "page": 0,
            "bbox": {"l": 10, "b": 20, "r": 100, "t": 40},
        }
    ]


def test_extract_bbox_accepts_docling_provenance_objects():
    prov = [
        SimpleNamespace(
            bbox=SimpleNamespace(l=10, b=20, r=100, t=40),
        )
    ]

    assert _extract_bbox(prov) == {"l": 10, "b": 20, "r": 100, "t": 40}


def test_normalize_lang_tag_maps_common_names_and_rejects_invalid_tokens():
    assert _normalize_lang_tag("English") == "en"
    assert _normalize_lang_tag("eng") == "en"
    assert _normalize_lang_tag("fr-ca") == "fr-CA"
    assert _normalize_lang_tag("not a language") is None


def test_normalize_docling_elements_normalizes_metadata_language_tags():
    doc_dict = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [
            {
                "label": "paragraph",
                "text": "Bonjour tout le monde ici present aujourd hui",
                "language": "French",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 20, "r": 100, "t": 40}}],
            }
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert elements[0]["lang"] == "fr"


def test_normalize_docling_elements_keeps_visible_contents_as_heading_and_paragraphs():
    doc_dict = {
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/texts/1"},
                {"$ref": "#/texts/2"},
            ],
        },
        "texts": [
            {
                "label": "section_header",
                "text": "Contents",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 180, "r": 180, "t": 195}}],
            },
            {
                "label": "paragraph",
                "text": "Introduction ........ 1",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 150, "r": 180, "t": 165}}],
            },
            {
                "label": "paragraph",
                "text": "Methods ........ 3",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 130, "r": 180, "t": 145}}],
            },
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert [element["type"] for element in elements] == ["heading", "paragraph", "paragraph"]
    assert [element["text"] for element in elements] == [
        "Contents",
        "Introduction ........ 1",
        "Methods ........ 3",
    ]


def test_normalize_docling_elements_keeps_toc_like_tables_as_tables():
    doc_dict = {
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/tables/0"},
                {"$ref": "#/texts/1"},
            ],
        },
        "texts": [
            {
                "label": "section_header",
                "text": "Contents",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 180, "r": 180, "t": 195}}],
            },
            {
                "label": "section_header",
                "text": "PDFlib GmbH ........ 7",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 120, "r": 180, "t": 135}}],
            },
        ],
        "tables": [
            {
                "label": "table",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 140, "r": 180, "t": 170}}],
                "data": {
                    "num_rows": 2,
                    "num_cols": 3,
                    "table_cells": [
                        {"text": "1", "start_row_offset_idx": 0, "start_col_offset_idx": 0},
                        {"text": "Introduction", "start_row_offset_idx": 0, "start_col_offset_idx": 1},
                        {"text": "3", "start_row_offset_idx": 0, "start_col_offset_idx": 2},
                        {"text": "2", "start_row_offset_idx": 1, "start_col_offset_idx": 0},
                        {"text": "Installation", "start_row_offset_idx": 1, "start_col_offset_idx": 1},
                        {"text": "6", "start_row_offset_idx": 1, "start_col_offset_idx": 2},
                    ],
                },
            },
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert [el["type"] for el in elements] == ["heading", "table", "heading"]
    assert elements[1]["cells"][0]["text"] == "1"
    assert elements[1]["cells"][1]["text"] == "Introduction"
    assert elements[2]["text"] == "PDFlib GmbH ........ 7"


def test_normalize_docling_elements_keeps_formula_like_paragraphs_as_paragraphs():
    doc_dict = {
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "texts": [
            {
                "label": "paragraph",
                "text": "f(x) = x^2 + 1",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 20, "r": 100, "t": 40}}],
            }
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert elements[0]["type"] == "paragraph"


def test_normalize_docling_elements_does_not_expand_toc_tables_into_items():
    doc_dict = {
        "body": {
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/tables/0"},
            ],
        },
        "texts": [
            {
                "label": "section_header",
                "text": "Table of Contents",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 180, "r": 200, "t": 195}}],
            },
        ],
        "tables": [
            {
                "label": "table",
                "prov": [{"page_no": 1, "bbox": {"l": 10, "b": 20, "r": 200, "t": 170}}],
                "data": {
                    "num_rows": 3,
                    "num_cols": 3,
                    "table_cells": [
                        {"text": "1.1", "start_row_offset_idx": 0, "start_col_offset_idx": 0},
                        {"text": "Introduction", "start_row_offset_idx": 0, "start_col_offset_idx": 1},
                        {"text": "3", "start_row_offset_idx": 0, "start_col_offset_idx": 2},
                        {"text": "1.2", "start_row_offset_idx": 1, "start_col_offset_idx": 0},
                        {"text": "Methods", "start_row_offset_idx": 1, "start_col_offset_idx": 1},
                        {"text": "6", "start_row_offset_idx": 1, "start_col_offset_idx": 2},
                        {"text": "1.3", "start_row_offset_idx": 2, "start_col_offset_idx": 0},
                        {"text": "Results", "start_row_offset_idx": 2, "start_col_offset_idx": 1},
                        {"text": "9", "start_row_offset_idx": 2, "start_col_offset_idx": 2},
                    ],
                },
            },
        ],
    }

    elements = _normalize_docling_elements(doc_dict)

    assert [el["type"] for el in elements] == ["heading", "table"]
    assert elements[0]["text"] == "Table of Contents"
    assert elements[1]["num_rows"] == 3
    assert elements[1]["num_cols"] == 3


def test_mark_toc_sequences_and_expand_tables_build_row_level_toc_items():
    elements = [
        {"type": "heading", "text": "Table of Contents", "page": 0},
        {
            "type": "table",
            "page": 0,
            "bbox": {"l": 10, "b": 20, "r": 200, "t": 170},
            "num_rows": 3,
            "num_cols": 3,
            "cells": [
                {"text": "1.1", "row": 0, "col": 0},
                {"text": "Introduction", "row": 0, "col": 1},
                {"text": "3", "row": 0, "col": 2},
                {"text": "1.2", "row": 1, "col": 0},
                {"text": "Methods", "row": 1, "col": 1},
                {"text": "6", "row": 1, "col": 2},
                {"text": "1.3", "row": 2, "col": 0},
                {"text": "Results", "row": 2, "col": 1},
                {"text": "9", "row": 2, "col": 2},
            ],
        },
    ]

    _mark_toc_sequences(elements)
    expanded = _expand_toc_item_tables(elements)

    assert [element["type"] for element in expanded] == [
        "toc_caption",
        "toc_item",
        "toc_item",
        "toc_item",
    ]
    assert [element["text"] for element in expanded[1:]] == [
        "1.1 Introduction 3",
        "1.2 Methods 6",
        "1.3 Results 9",
    ]
    assert all(element.get("toc_group_ref") == "toc-0" for element in expanded)


def test_normalize_native_toc_preserves_hierarchy_and_discards_empty_nodes():
    native_toc = {
        "text": " <root> ",
        "children": [
            {"text": " Cover ", "children": []},
            {
                "text": " Glossaries ",
                "children": [
                    {"text": " Abbreviations and Acronyms ", "children": []},
                    {"text": " ", "children": []},
                ],
            },
        ],
    }

    normalized = _normalize_native_toc(native_toc)

    assert normalized == {
        "text": "<root>",
        "children": [
            {"text": "Cover", "children": []},
            {
                "text": "Glossaries",
                "children": [
                    {"text": "Abbreviations and Acronyms", "children": []},
                ],
            },
        ],
    }


def test_toc_rows_from_word_cells_reconstructs_visible_rows():
    word_cells = [
        {"text": "TABLE", "bbox": {"l": 54.0, "b": 690.0, "r": 107.0, "t": 705.0}},
        {"text": "OF", "bbox": {"l": 113.0, "b": 690.0, "r": 135.0, "t": 705.0}},
        {"text": "CONTENTS", "bbox": {"l": 141.0, "b": 690.0, "r": 233.0, "t": 705.0}},
        {"text": "GLOSSARIES", "bbox": {"l": 49.0, "b": 586.0, "r": 120.0, "t": 596.0}},
        {"text": "Abbreviations", "bbox": {"l": 54.0, "b": 568.0, "r": 130.0, "t": 579.0}},
        {"text": "and", "bbox": {"l": 133.0, "b": 568.0, "r": 150.0, "t": 579.0}},
        {"text": "Acronyms", "bbox": {"l": 153.0, "b": 568.0, "r": 215.0, "t": 579.0}},
        {"text": ".", "bbox": {"l": 220.0, "b": 568.0, "r": 223.0, "t": 579.0}},
        {"text": ".", "bbox": {"l": 232.0, "b": 568.0, "r": 235.0, "t": 579.0}},
        {"text": "viii", "bbox": {"l": 520.0, "b": 568.0, "r": 540.0, "t": 579.0}},
    ]

    rows = _toc_rows_from_word_cells(word_cells)

    assert [row["text"] for row in rows] == [
        "TABLE OF CONTENTS",
        "GLOSSARIES",
        "Abbreviations and Acronyms . . viii",
    ]


def test_rebuild_toc_elements_from_page_rows_replaces_malformed_entries():
    elements = [
        {"type": "heading", "text": "Management Track Assessments Fall 2022", "page": 0},
        {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 3, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "1.1. Executive . . . . . . . . . . . . . . . . 1", "page": 3, "toc_group_ref": "toc-0"},
        {"type": "toc_item", "text": "5.1. Reviewer Comments: Georges . . . . . . . . . . . . . . 52", "page": 3, "toc_group_ref": "toc-0"},
    ]
    page_rows = {
        3: [
            {"text": "TABLE OF CONTENTS", "bbox": {"l": 50.0, "b": 690.0, "r": 230.0, "t": 705.0}},
            {"text": "GLOSSARIES", "bbox": {"l": 49.0, "b": 586.0, "r": 120.0, "t": 596.0}},
            {"text": "Abbreviations and Acronyms . . . . . . . . . . viii", "bbox": {"l": 54.0, "b": 568.0, "r": 540.0, "t": 579.0}},
            {"text": "1.1. Executive Summary . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 1", "bbox": {"l": 54.0, "b": 454.0, "r": 540.0, "t": 465.0}},
            {"text": "5.1. Reviewer Comments: Georges Bank haddock . . . . . . . . . . . . . . . . . . . 52", "bbox": {"l": 54.0, "b": 215.0, "r": 540.0, "t": 226.0}},
        ],
    }

    rebuilt = _rebuild_toc_elements_from_page_rows(elements, page_rows)

    toc_items = [element for element in rebuilt if element.get("type") == "toc_item"]
    assert [item["text"] for item in toc_items] == [
        "GLOSSARIES",
        "Abbreviations and Acronyms . . . . . . . . . . viii",
        "1.1. Executive Summary . . . . . . . . . . . . . . . . . . . . . . . . . . . . . 1",
        "5.1. Reviewer Comments: Georges Bank haddock . . . . . . . . . . . . . . . . . . . 52",
    ]
    assert toc_items[0]["toc_group_heading"] is True


def test_extract_title_from_docling_uses_only_docling_title_labels():
    doc_dict = {
        "texts": [
            {
                "label": "title",
                "text": "C H A P T E R 5",
            }
        ]
    }

    title = _extract_title_from_docling(doc_dict, elements=[])

    assert title == "CHAPTER 5"
