import asyncio

from app.services.title_intelligence import enhance_document_title_with_intelligence


def test_title_intelligence_applies_visible_title_when_missing(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        assert schema_name == "document_title_extraction"
        assert cache_breakpoint_index == 1
        context = next(part for part in content if part.get("type") == "text" and "Title extraction context:" in part.get("text", ""))
        assert "title_candidates" in context["text"]
        return {
            "task_type": "document_title_extraction",
            "summary": "Recovered the visible title from the first-page heading run.",
            "confidence": "high",
            "reason": "The first-page heading fragments form one clear chapter title.",
            "title": "CHAPTER 5 Physical Development: The Brain, Body, Motor Skills, and Sexual Development",
        }

    monkeypatch.setattr(
        "app.services.title_intelligence.request_llm_json",
        _fake_request_llm_json,
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
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index):
        assert schema_name == "document_title_extraction"
        assert cache_breakpoint_index == 1
        context = next(part for part in content if part.get("type") == "text" and "Title extraction context:" in part.get("text", ""))
        assert "Existing Title" in context["text"]
        return {
            "task_type": "document_title_extraction",
            "summary": "The current title already appears to be the best visible title.",
            "confidence": "low",
            "reason": "No clearer visible title was found.",
            "title": "",
        }

    monkeypatch.setattr(
        "app.services.title_intelligence.request_llm_json",
        _fake_request_llm_json,
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
