import asyncio

import pikepdf

from app.services.early_pages_intelligence import (
    enhance_document_title_and_front_matter_with_intelligence,
)


def _make_pdf(pdf_path, page_count=4):
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))


def test_early_pages_intelligence_combines_title_and_front_matter(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=4)

    async def _fake_direct_request(**kwargs):
        assert kwargs["page_numbers"] == [1, 2, 3]
        assert kwargs["context_payload"]["job_filename"] == "report.pdf"
        assert kwargs["context_payload"]["title_candidates"]
        assert kwargs["context_payload"]["front_matter_pages"]
        return {
            "task_type": "document_early_pages_intelligence",
            "summary": "Recovered the title and pre-TOC page roles from the same early pages.",
            "title": {
                "confidence": "high",
                "reason": "The cover page and first heading run show a clear title.",
                "title": "Management Track Assessments Spring 2023",
            },
            "front_matter": {
                "confidence": "high",
                "reason": "The three pages before the TOC act as cover, inside-cover, and series information.",
                "entries": [
                    {"page_index": 0, "label": "Cover"},
                    {"page_index": 1, "label": "Inside-Cover page"},
                    {"page_index": 2, "label": "Series Information"},
                ],
            },
        }

    monkeypatch.setattr(
        "app.services.early_pages_intelligence.request_direct_gemini_pdf_json",
        _fake_direct_request,
    )

    structure_json = {
        "title": None,
        "elements": [
            {"type": "heading", "text": "Management Track Assessments Spring 2023", "page": 0, "level": 1},
            {"type": "heading", "text": "U.S. Department of Commerce", "page": 1, "level": 1},
            {"type": "heading", "text": "NOAA Technical Memorandum, Editorial Notes", "page": 2, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 3, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 3, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 4, "level": 1},
        ],
    }

    updated, title_audit, front_matter_entries, front_matter_audit = asyncio.run(
        enhance_document_title_and_front_matter_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["title"] == "Management Track Assessments Spring 2023"
    assert title_audit["applied"] is True
    assert front_matter_audit["applied"] is True
    assert [entry["text"] for entry in front_matter_entries] == [
        "Cover",
        "Inside-Cover page",
        "Series Information",
    ]

def test_early_pages_intelligence_uses_direct_gemini(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path, page_count=4)

    async def _fake_direct_request(**kwargs):
        assert kwargs["page_numbers"] == [1, 2]
        assert "title_candidates" in kwargs["context_payload"]
        assert "front_matter_pages" in kwargs["context_payload"]
        return {
            "task_type": "document_early_pages_intelligence",
            "summary": "Recovered the title and front matter from direct Gemini.",
            "title": {
                "confidence": "high",
                "reason": "Clear cover title.",
                "title": "Management Track Assessments Spring 2023",
            },
            "front_matter": {
                "confidence": "high",
                "reason": "Visible cover before TOC.",
                "entries": [{"page_index": 0, "label": "Cover"}],
            },
        }

    monkeypatch.setattr(
        "app.services.early_pages_intelligence.request_direct_gemini_pdf_json",
        _fake_direct_request,
    )

    structure_json = {
        "title": None,
        "elements": [
            {"type": "heading", "text": "Management Track Assessments Spring 2023", "page": 0, "level": 1},
            {"type": "heading", "text": "NOAA Technical Memorandum, Editorial Notes", "page": 1, "level": 1},
            {"type": "toc_caption", "text": "TABLE OF CONTENTS", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "toc_item", "text": "1 Panel Report", "page": 2, "toc_group_ref": "toc-0"},
            {"type": "heading", "text": "1 Panel Report", "page": 3, "level": 1},
        ],
    }

    updated, title_audit, front_matter_entries, front_matter_audit = asyncio.run(
        enhance_document_title_and_front_matter_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="report.pdf",
            llm_client=object(),
        )
    )

    assert updated["title"] == "Management Track Assessments Spring 2023"
    assert title_audit["applied"] is True
    assert front_matter_audit["applied"] is True
    assert [entry["text"] for entry in front_matter_entries] == ["Cover"]
