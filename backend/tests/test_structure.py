from app.pipeline.structure import _normalize_docling_elements, _normalize_lang_tag


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


def test_normalize_docling_elements_marks_table_of_contents_sequences():
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

    assert elements[0]["type"] == "toc_caption"
    assert elements[1]["type"] == "toc_item"
    assert elements[2]["type"] == "toc_item"
    assert elements[0]["toc_group_ref"] == elements[1]["toc_group_ref"] == elements[2]["toc_group_ref"]


def test_normalize_docling_elements_marks_toc_tables_and_heading_entries():
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

    assert elements[0]["type"] == "toc_caption"
    assert [el["type"] for el in elements[1:4]] == ["toc_item", "toc_item", "toc_item"]
    assert elements[1]["text"] == "1 Introduction 3"
    assert elements[2]["text"] == "2 Installation 6"
    assert elements[3]["text"] == "PDFlib GmbH ........ 7"
    assert elements[0]["toc_group_ref"] == elements[1]["toc_group_ref"] == elements[2]["toc_group_ref"] == elements[3]["toc_group_ref"]


def test_normalize_docling_elements_splits_toc_table_rows_into_separate_items():
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

    assert [el["type"] for el in elements] == ["toc_caption", "toc_item", "toc_item", "toc_item"]
    assert [el["text"] for el in elements[1:]] == [
        "1.1 Introduction 3",
        "1.2 Methods 6",
        "1.3 Results 9",
    ]
    assert all(elements[idx]["bbox"]["t"] > elements[idx]["bbox"]["b"] for idx in (1, 2, 3))
