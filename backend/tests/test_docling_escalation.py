import pikepdf

from app.services import docling_escalation
from app.services.docling_escalation import (
    docling_pretag_ambiguity_router,
    docling_structure_escalation_plan,
    docling_table_targets_for_intelligence,
)


def test_docling_structure_escalation_prefers_docling_title_and_native_toc():
    plan = docling_structure_escalation_plan(
        {
            "title": "Accessible PDF Manual",
            "language": "en",
            "native_toc": {
                "text": "",
                "children": [{"text": "1 Intro", "children": []}],
            },
        }
    )

    assert plan["title"]["decision"] == "docling"
    assert plan["title"]["reason"] == "docling_title_present"
    assert plan["bookmarks"]["decision"] == "docling"
    assert plan["bookmarks"]["reason"] == "docling_native_toc_present"
    assert plan["toc"]["decision"] == "gemini"
    assert plan["language"]["decision"] == "docling"


def test_docling_structure_escalation_uses_gemini_when_docling_signals_are_missing():
    plan = docling_structure_escalation_plan(
        {
            "title": "",
            "elements": [],
        }
    )

    assert plan["title"]["decision"] == "gemini"
    assert plan["title"]["reason"] == "missing_docling_title"
    assert plan["bookmarks"]["decision"] == "gemini"
    assert plan["bookmarks"]["reason"] == "missing_docling_native_toc"


def test_docling_table_targets_route_tables_missing_headers():
    targets = docling_table_targets_for_intelligence(
        {
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "review_id": "review-table-1",
                    "num_rows": 3,
                    "num_cols": 3,
                    "bbox": {"l": 0, "t": 200, "r": 200, "b": 0},
                    "cells": [
                        {"row": 0, "col": 0, "text": "Name"},
                        {"row": 0, "col": 1, "text": "Value"},
                        {"row": 1, "col": 0, "text": "A"},
                        {"row": 1, "col": 1, "text": "1"},
                    ],
                }
            ]
        }
    )

    assert len(targets) == 1
    assert targets[0]["table_review_id"] == "review-table-1"
    assert targets[0]["risk_reasons"] == ["missing_docling_simple_headers"]


def test_docling_table_targets_skip_simple_headered_tables():
    targets = docling_table_targets_for_intelligence(
        {
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "review_id": "review-table-1",
                    "num_rows": 3,
                    "num_cols": 3,
                    "bbox": {"l": 0, "t": 200, "r": 200, "b": 0},
                    "cells": [
                        {"row": 0, "col": 0, "text": "Name", "column_header": True},
                        {"row": 0, "col": 1, "text": "Value", "column_header": True},
                        {"row": 1, "col": 0, "text": "A"},
                        {"row": 1, "col": 1, "text": "1"},
                    ],
                }
            ]
        }
    )

    assert targets == []


def test_docling_pretag_ambiguity_router_prefers_docling_when_no_targets(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))

    monkeypatch.setattr(docling_escalation, "form_targets_for_intelligence", lambda **_: [])
    monkeypatch.setattr(docling_escalation, "widget_targets_for_rationalization", lambda **_: [])
    monkeypatch.setattr(docling_escalation, "docling_table_targets_for_intelligence", lambda *_: [])

    router = docling_pretag_ambiguity_router(
        working_pdf=pdf_path,
        structure_json={"elements": []},
    )

    assert router["plan"]["forms"]["decision"] == "docling"
    assert router["plan"]["tables"]["decision"] == "docling"
    assert router["plan"]["widgets"]["decision"] == "docling"
    assert router["form_targets"] == []
    assert router["table_targets"] == []
    assert router["widget_targets"] == []


def test_docling_pretag_ambiguity_router_routes_only_unresolved_units(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))

    monkeypatch.setattr(
        docling_escalation,
        "form_targets_for_intelligence",
        lambda **_: [{"field_review_id": "field-1", "page": 1}],
    )
    monkeypatch.setattr(
        docling_escalation,
        "widget_targets_for_rationalization",
        lambda **_: [{"field_review_id": "widget-1", "page": 2}],
    )
    monkeypatch.setattr(
        docling_escalation,
        "docling_table_targets_for_intelligence",
        lambda *_: [{"table_review_id": "table-1", "page": 3}],
    )

    router = docling_pretag_ambiguity_router(
        working_pdf=pdf_path,
        structure_json={"elements": []},
    )

    assert router["plan"]["forms"]["decision"] == "gemini"
    assert router["plan"]["forms"]["candidate_count"] == 1
    assert router["plan"]["forms"]["pages"] == [1]
    assert router["plan"]["tables"]["decision"] == "gemini"
    assert router["plan"]["tables"]["pages"] == [3]
    assert router["plan"]["widgets"]["decision"] == "gemini"
    assert router["plan"]["widgets"]["pages"] == [2]
