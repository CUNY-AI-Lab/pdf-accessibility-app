import asyncio
from types import SimpleNamespace

from app.services.intelligence_gemini_pages import generate_suspicious_text_intelligence
from app.services.semantic_units import SemanticDecision


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def test_generate_suspicious_text_intelligence_returns_normalized_blocks(monkeypatch, tmp_path):
    captured = {}

    async def _fake_adjudicate(*, job, units, llm_client):
        captured["units"] = units
        return [
            SemanticDecision(
                unit_id="review-1",
                unit_type="text_block",
                summary="Page title is readable despite broken extraction.",
                confidence="high",
                confidence_score=0.9,
                suggested_action="set_resolved_text",
                reason="Visible title is clear, but extracted text is split by spacing.",
                chosen_source="ocr",
                resolved_text="Data Book",
                issue_type="spacing_only",
                should_block_accessibility=True,
            )
        ]

    monkeypatch.setattr(
        "app.services.intelligence_gemini_pages.adjudicate_semantic_units",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_suspicious_text_intelligence(
            job=_job(tmp_path),
            page_numbers=[1],
            suspicious_blocks=[
                {
                    "page": 1,
                    "review_id": "review-1",
                    "role": "heading",
                    "text": "D a t a  B o o k",
                    "extracted_text": "D a t a  B o o k",
                    "original_text_candidate": "D a t a  B o o k",
                    "native_text_candidate": "D a t a  B o o k",
                    "ocr_text_candidate": "Data Book",
                    "bbox": {"l": 10, "t": 20, "r": 100, "b": 40},
                    "signals": ["letters separated by spaces"],
                }
            ],
            llm_client=object(),
        )
    )

    assert result["task_type"] == "page_text_intelligence"
    assert result["summary"] == "Page title is readable despite broken extraction."
    assert result["confidence_score"] == 0.9
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert block["page"] == 1
    assert block["review_id"] == "review-1"
    assert block["readable_text_hint"] == "Data Book"
    assert block["chosen_source"] == "ocr"
    assert block["issue_type"] == "spacing_only"
    assert block["confidence"] == "high"
    assert block["should_block_accessibility"] is True
    assert block["reason"] == "Visible title is clear, but extracted text is split by spacing."
    assert block["role"] == "heading"
    assert block["native_text_candidate"] == "D a t a  B o o k"
    assert block["original_text_candidate"] == "D a t a  B o o k"
    assert block["extracted_text"] == "D a t a  B o o k"
    assert block["ocr_text_candidate"] == "Data Book"

    unit = captured["units"][0]
    assert unit.unit_type == "text_block"
    assert unit.unit_id == "review-1"
    assert unit.native_text_candidate == "D a t a  B o o k"
    assert unit.ocr_text_candidate == "Data Book"
    assert unit.metadata["signals"] == ["letters separated by spaces"]
