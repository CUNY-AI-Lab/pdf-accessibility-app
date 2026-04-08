import asyncio
from types import SimpleNamespace

import pikepdf

from app.services.intelligence_gemini_reading_order import (
    generate_reading_order_intelligence,
)


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def test_generate_reading_order_intelligence_uses_pdf_file_input(monkeypatch, tmp_path):
    captured = {}

    async def _fake_request_llm_json(
        *,
        llm_client,
        content,
        schema_name=None,
        response_schema=None,
        cache_breakpoint_index=None,
    ):
        captured["content"] = content
        captured["schema_name"] = schema_name
        captured["cache_breakpoint_index"] = cache_breakpoint_index
        return {
            "task_type": "reading_order_intelligence",
            "summary": "The current order is already correct.",
            "confidence": "high",
            "suggested_action": "confirm_current_order",
            "reason": "The visible page flow matches the current block order.",
            "page": 1,
            "ordered_review_ids": [],
            "element_updates": [],
        }

    monkeypatch.setattr(
        "app.services.intelligence_gemini_reading_order.request_llm_json",
        _fake_request_llm_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_reading_order.pdf_file_parts",
        lambda job, page_numbers, filename=None: [
            {
                "type": "file",
                "file": {
                    "filename": filename or "sample.pdf",
                    "file_data": "data:application/pdf;base64,page",
                },
            }
        ],
    )

    result = asyncio.run(
        generate_reading_order_intelligence(
            job=_job(tmp_path),
            page_number=1,
            page_blocks={
                "page": 1,
                "review_items": [
                    {"review_id": "review-1", "type": "heading", "text": "Introduction"},
                    {"review_id": "review-2", "type": "paragraph", "text": "First paragraph"},
                ],
            },
            page_structure_fragments=[{"page": 1, "type": "heading", "text": "Introduction"}],
            page_text_intelligence_blocks=[],
            llm_client=object(),
        )
    )

    assert captured["schema_name"] is None
    assert captured["cache_breakpoint_index"] == 1
    assert captured["content"][1]["type"] == "file"
    assert "one PDF page input only" in captured["content"][0]["text"]
    assert result["suggested_action"] == "confirm_current_order"
    assert result["confidence"] == "high"
    assert result["page"] == 1
