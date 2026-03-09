from pathlib import Path
from types import SimpleNamespace

import pikepdf

from app.services.document_intelligence import (
    build_document_model,
    collect_nearby_blocks,
    collect_structure_fragments,
)


def test_build_document_model_normalizes_blocks_and_tables():
    job = SimpleNamespace(
        structure_json="""
        {
          "title": "Sample Doc",
          "elements": [
            {
              "review_id": "review-1",
              "type": "heading",
              "page": 0,
              "level": 2,
              "text": " Heading One ",
              "bbox": {"l": 10, "t": 20, "r": 100, "b": 40}
            },
            {
              "review_id": "review-2",
              "type": "table",
              "page": 0,
              "text": "Quarterly results",
              "num_rows": 2,
              "num_cols": 2,
              "bbox": {"l": 15, "t": 60, "r": 180, "b": 140},
              "cells": [
                {"row": 0, "col": 0, "text": "Quarter", "column_header": true},
                {"row": 0, "col": 1, "text": "Revenue", "column_header": true},
                {"row": 1, "col": 0, "text": "Q1", "row_header": true},
                {"row": 1, "col": 1, "text": "$10"}
              ]
            }
          ]
        }
        """,
    )

    document = build_document_model(job=job)

    assert document.title == "Sample Doc"
    assert len(document.pages) == 1
    page = document.pages[0]
    assert page.page_number == 1
    assert len(page.blocks) == 1
    assert page.blocks[0].review_id == "review-1"
    assert page.blocks[0].role == "heading"
    assert page.blocks[0].text == "Heading One"
    assert page.blocks[0].native_text_candidate == "Heading One"
    assert page.blocks[0].bbox is not None
    assert len(page.tables) == 1
    assert page.tables[0].table_review_id == "review-2"
    assert page.tables[0].header_rows == [0]
    assert page.tables[0].row_header_columns == [0]
    assert len(page.tables[0].cells) == 4


def test_collect_structure_fragments_uses_document_model():
    document = build_document_model(
        structure_json={
            "elements": [
                {"review_id": "review-1", "type": "paragraph", "page": 0, "text": "This is a sufficiently long paragraph for review."},
                {"review_id": "review-2", "type": "paragraph", "page": 0, "text": "This is a sufficiently long paragraph for review."},
                {"review_id": "review-3", "type": "paragraph", "page": 1, "text": "Another distinct fragment appears on the next page."},
            ]
        }
    )

    fragments = collect_structure_fragments(document, max_fragments=10)

    assert fragments == [
        {
            "page": 1,
            "type": "paragraph",
            "text": "This is a sufficiently long paragraph for review.",
            "bbox": None,
            "review_id": "review-1",
            "provenance": "legacy_structure",
            "confidence": 0.5,
        },
        {
            "page": 2,
            "type": "paragraph",
            "text": "Another distinct fragment appears on the next page.",
            "bbox": None,
            "review_id": "review-3",
            "provenance": "legacy_structure",
            "confidence": 0.5,
        },
    ]


def test_build_document_model_carries_resolved_text_metadata():
    document = build_document_model(
        structure_json={
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "heading",
                    "page": 0,
                    "text": "A B S T R A C T",
                    "actual_text": "ABSTRACT",
                    "resolution_source": "pretag_ocr",
                    "resolution_reason": "OCR resolves the heading cleanly.",
                    "semantic_issue_type": "spacing_only",
                }
            ]
        }
    )

    block = document.pages[0].blocks[0]
    assert block.text == "A B S T R A C T"
    assert block.native_text_candidate == "A B S T R A C T"
    assert block.resolved_text == "ABSTRACT"
    assert block.semantic_text_hint == "ABSTRACT"
    assert block.resolution_source == "pretag_ocr"
    assert block.resolution_reason == "OCR resolves the heading cleanly."
    assert block.semantic_issue_type == "spacing_only"


def _pdf_with_widget(path: Path) -> None:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    widget = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Widget"),
                "/Rect": pikepdf.Array([10, 10, 80, 28]),
                "/FT": pikepdf.Name("/Tx"),
                "/T": pikepdf.String("f1_01[0]"),
            }
        )
    )
    page["/Annots"] = pikepdf.Array([widget])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({"/Fields": pikepdf.Array([widget])})
    pdf.save(path)


def test_build_document_model_includes_widget_fields(tmp_path):
    pdf_path = tmp_path / "widget.pdf"
    _pdf_with_widget(pdf_path)

    document = build_document_model(
        structure_json={
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "paragraph",
                    "page": 0,
                    "text": "First name and middle initial",
                    "bbox": {"l": 10, "t": 50, "r": 140, "b": 70},
                }
            ]
        },
        pdf_path=pdf_path,
    )

    assert len(document.pages) == 1
    page = document.pages[0]
    assert len(page.fields) == 1
    field = page.fields[0]
    assert field.field_type == "text"
    assert field.field_name == "f1_01[0]"
    assert field.label_quality == "missing"

    nearby = collect_nearby_blocks(document, page_number=1, bbox=field.bbox.to_dict(), limit=3)
    assert nearby[0]["review_id"] == "review-1"
