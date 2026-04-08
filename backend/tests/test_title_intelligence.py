import asyncio

import pikepdf

from app.services.title_intelligence import enhance_document_title_with_intelligence


def _make_pdf(pdf_path, page_count=2):
    pdf = pikepdf.Pdf.new()
    for _ in range(page_count):
        pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))


def test_title_intelligence_applies_visible_title_when_missing(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)

    async def _fake_direct_request(**kwargs):
        assert kwargs["page_numbers"] == [1, 2]
        assert kwargs["context_payload"]["job_filename"] == "chapter.pdf"
        assert kwargs["context_payload"]["current_title"] == ""
        assert kwargs["context_payload"]["title_candidates"]
        return {
            "task_type": "document_title_extraction",
            "summary": "Recovered the visible title from the first-page heading run.",
            "confidence": "high",
            "reason": "The first-page heading fragments form one clear chapter title.",
            "title": "CHAPTER 5 Physical Development: The Brain, Body, Motor Skills, and Sexual Development",
        }

    monkeypatch.setattr(
        "app.services.title_intelligence.request_direct_gemini_pdf_json",
        _fake_direct_request,
    )

    structure_json = {
        "title": None,
        "elements": [
            {"type": "heading", "text": "C H A P T E R 5", "page": 0, "bbox": {"l": 80, "b": 594, "r": 153, "t": 717}},
            {"type": "heading", "text": "Physical Development:", "page": 0, "bbox": {"l": 216, "b": 671, "r": 563, "t": 702}},
            {
                "type": "heading",
                "text": "The Brain, Body, Motor Skills, and Sexual Development",
                "page": 0,
                "bbox": {"l": 216, "b": 611, "r": 539, "t": 660},
            },
        ],
    }

    updated, audit = asyncio.run(
        enhance_document_title_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="chapter.pdf",
            llm_client=object(),
        )
    )

    assert updated["title"] == "CHAPTER 5 Physical Development: The Brain, Body, Motor Skills, and Sexual Development"
    assert audit["applied"] is True
    assert audit["confidence"] == "high"


def test_title_intelligence_keeps_existing_title_when_model_declines(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    _make_pdf(pdf_path)

    async def _fake_direct_request(**kwargs):
        assert kwargs["page_numbers"] == [1, 2]
        assert kwargs["context_payload"]["current_title"] == "Existing Title"
        return {
            "task_type": "document_title_extraction",
            "summary": "The current title already appears to be the best visible title.",
            "confidence": "low",
            "reason": "No clearer visible title was found.",
            "title": "",
        }

    monkeypatch.setattr(
        "app.services.title_intelligence.request_direct_gemini_pdf_json",
        _fake_direct_request,
    )

    structure_json = {
        "title": "Existing Title",
        "elements": [
            {"type": "heading", "text": "Existing Title", "page": 0},
        ],
    }

    updated, audit = asyncio.run(
        enhance_document_title_with_intelligence(
            pdf_path=pdf_path,
            structure_json=structure_json,
            original_filename="doc.pdf",
            llm_client=object(),
        )
    )

    assert updated["title"] == "Existing Title"
    assert audit["attempted"] is True
    assert audit["applied"] is False
    assert audit["retained_existing_title"] is True
    assert audit["title"] == "Existing Title"
