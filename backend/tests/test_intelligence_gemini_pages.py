import asyncio
import json
from types import SimpleNamespace

from app.services import intelligence_gemini_pages
from app.services.intelligence_gemini_pages import generate_suspicious_text_intelligence


class _FakeLlmClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def chat_completion(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.payload),
                    }
                }
            ]
        }


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def test_generate_suspicious_text_intelligence_returns_normalized_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intelligence_gemini_pages,
        "render_page_png_data_url",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        intelligence_gemini_pages,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:block-{page_number}",
    )

    fake_llm = _FakeLlmClient(
        {
            "task_type": "page_text_intelligence",
            "summary": "Page title is readable despite broken extraction.",
            "confidence": "high",
            "blocks": [
                {
                    "page": 1,
                    "review_id": "review-1",
                    "readable_text_hint": "Data Book",
                    "chosen_source": "ocr",
                    "issue_type": "spacing_only",
                    "confidence": "high",
                    "should_block_accessibility": True,
                    "reason": "Visible title is clear, but extracted text is split by spacing.",
                }
            ],
        }
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
            llm_client=fake_llm,
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
    assert fake_llm.calls
    prompt = fake_llm.calls[0]["messages"][0]["content"][0]["text"]
    assert '"native_text_candidate": "D a t a  B o o k"' in prompt
    assert '"ocr_text_candidate": "Data Book"' in prompt
