from pathlib import Path

from app.pipeline import structure


def test_infer_document_language_prefers_weighted_element_language():
    elements = [
        {"type": "heading", "text": "Bonjour tout le monde", "lang": "fr-CA"},
        {
            "type": "paragraph",
            "text": "Bonjour encore. Ce document est redige en francais canadien.",
            "lang": "fr-CA",
        },
        {"type": "paragraph", "text": "Hello world", "lang": "en"},
    ]

    assert structure._infer_document_language(elements, "Titre") == "fr-CA"


def test_infer_document_language_falls_back_to_visible_text_detection(monkeypatch):
    elements = [
        {
            "type": "paragraph",
            "text": "This document explains how to complete the form and submit it safely.",
        },
        {
            "type": "paragraph",
            "text": "Read each section carefully before entering your information.",
        },
    ]

    monkeypatch.setattr(structure, "_detect_language", lambda _text: "en")

    assert structure._infer_document_language(elements, "Form instructions") == "en"


def test_extract_title_from_docling_ignores_late_table_caption_titles():
    doc_dict = {
        "texts": [
            {
                "label": "title",
                "text": "Table 2: Stocks reviewed at September 2022 meeting",
                "prov": [{"page_no": 18}],
            }
        ]
    }
    elements = [
        {
            "type": "heading",
            "level": 1,
            "page": 0,
            "text": "Management Track Assessments Completed in Fall 2022",
        }
    ]

    assert (
        structure._extract_title_from_docling(doc_dict, elements)
        == "Management Track Assessments Completed in Fall 2022"
    )


def test_extract_title_from_docling_keeps_early_docling_title_when_plausible():
    doc_dict = {
        "texts": [
            {
                "label": "title",
                "text": "Creating Accessible PDF Documents",
                "prov": [{"page_no": 1}],
            }
        ]
    }
    elements = [
        {"type": "heading", "level": 1, "page": 0, "text": "Contents"},
        {"type": "heading", "level": 1, "page": 1, "text": "Introduction"},
    ]

    assert structure._extract_title_from_docling(doc_dict, elements) == "Creating Accessible PDF Documents"


def test_extract_title_from_docling_repairs_ligature_split_from_first_page(monkeypatch):
    doc_dict = {
        "texts": [
            {
                "label": "title",
                "text": "Fully Accessible PDF/UA documents. Case study: NOAA fi sh stock reports",
                "prov": [{"page_no": 1}],
            }
        ]
    }

    monkeypatch.setattr(
        structure,
        "_extract_first_page_lines",
        lambda _pdf_path: [
            "Fully Accessible PDF/UA documents.",
            "Case study: NOAA fish stock reports",
            "Ross Moore",
        ],
    )

    assert (
        structure._extract_title_from_docling(doc_dict, pdf_path=Path("/tmp/sample.pdf"))
        == "Fully Accessible PDF/UA documents. Case study: NOAA fish stock reports"
    )


def test_looks_like_toc_entry_requires_delimited_page_marker():
    assert not structure._looks_like_toc_entry("Abbreviations for fish stocks reviewed")
    assert structure._looks_like_toc_entry("Abbreviations for fish stocks reviewed ........ ii")
