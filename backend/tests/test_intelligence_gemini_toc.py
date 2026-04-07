import asyncio

from app.services.intelligence_gemini_toc import generate_toc_group_intelligence
from app.services.semantic_units import SemanticDecision


def test_generate_toc_group_intelligence_maps_entry_indexes(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    captured = {}

    async def _fake_adjudicate(*, job, unit, llm_client):
        captured["unit"] = unit
        return SemanticDecision(
            unit_id="toc-group-4",
            unit_type="toc_group",
            summary="This is a real TOC spanning two pages.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_toc_entries",
            reason="The candidate rows are TOC entries with page numbers.",
            is_toc=True,
            entry_indexes=[5, 6],
            entry_types={"5": "toc_item", "6": "toc_item_table"},
            caption_text_override="TABLE OF CONTENTS",
            entry_text_overrides={"5": "Introduction", "6": "Appendix material"},
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_toc.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_toc_group_intelligence(
            pdf_path=pdf_path,
            original_filename="doc.pdf",
            candidate_group={
                "caption_index": 4,
                "caption_text": "Contents",
                "pages": [1, 2],
                "candidate_elements": [
                    {"index": 5, "type": "paragraph", "text": "Intro .... 1"},
                    {"index": 6, "type": "table", "text": "Appendix table"},
                ],
            },
            llm_client=object(),
        )
    )

    assert result == {
        "caption_index": 4,
        "is_toc": True,
        "confidence": "high",
        "reason": "The candidate rows are TOC entries with page numbers.",
        "entry_indexes": [5, 6],
        "entry_types": {"5": "toc_item", "6": "toc_item_table"},
        "caption_text_override": "TABLE OF CONTENTS",
        "entry_text_overrides": {"5": "Introduction", "6": "Appendix material"},
    }
    unit = captured["unit"]
    assert unit.unit_type == "toc_group"
    assert unit.metadata["caption_index"] == 4
    assert unit.metadata["extra_page_numbers"] == [2]


def test_generate_toc_group_intelligence_defaults_to_all_candidates_for_positive_toc_without_indexes(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")

    async def _fake_adjudicate(*, job, unit, llm_client):
        return SemanticDecision(
            unit_id="toc-group-4",
            unit_type="toc_group",
            summary="This is a TOC.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_toc_entries",
            reason="The candidate rows are the TOC entries.",
            is_toc=True,
            entry_indexes=[],
            entry_types={},
            entry_text_overrides={"5": "Intro", "999": "Ignore me"},
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_toc.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_toc_group_intelligence(
            pdf_path=pdf_path,
            original_filename="doc.pdf",
            candidate_group={
                "caption_index": 4,
                "caption_text": "Contents",
                "pages": [1, 2],
                "candidate_elements": [
                    {"index": 5, "type": "paragraph", "text": "Intro .... 1"},
                    {"index": 6, "type": "table", "text": "Appendix table"},
                ],
            },
            llm_client=object(),
        )
    )

    assert result["entry_indexes"] == [5, 6]
    assert result["entry_types"] == {"5": "toc_item", "6": "toc_item_table"}
    assert result["entry_text_overrides"] == {"5": "Intro"}
