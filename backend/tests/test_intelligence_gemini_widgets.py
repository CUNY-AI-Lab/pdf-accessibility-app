import asyncio
from types import SimpleNamespace

import pikepdf

from app.services.intelligence_gemini_widgets import (
    generate_widget_intelligence,
    generate_widget_intelligence_for_page,
)


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.add_blank_page(page_size=(200, 200))
    pdf.save(str(pdf_path))
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def test_generate_widget_intelligence_uses_backend_aware_page_input(monkeypatch, tmp_path):
    captured = {}

    async def _fake_request_llm_json(
        *,
        llm_client,
        content,
        schema_name=None,
        response_schema=None,
        cache_breakpoint_index=None,
        conversation_prefix=None,
    ):
        captured["content"] = content
        captured["schema_name"] = schema_name
        captured["cache_breakpoint_index"] = cache_breakpoint_index
        return {
            "task_type": "widget_page_intelligence",
            "page": 1,
            "summary": "This widget looks like real interactive content.",
            "decisions": [
                {
                    "field_review_id": "field-widget-10-0",
                    "summary": "The control looks interactive.",
                    "confidence": "high",
                    "suggested_action": "preserve_control",
                    "reason": "The widget is positioned like a real form control.",
                }
            ],
        }

    monkeypatch.setattr(
        "app.services.intelligence_gemini_widgets.request_llm_json",
        _fake_request_llm_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_widgets.semantic_page_parts",
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
        generate_widget_intelligence(
            job=_job(tmp_path),
            target={
                "field_review_id": "field-widget-10-0",
                "page": 1,
                "field_type": "text",
                "field_name": "f1_01[0]",
                "accessible_name": "Name",
                "bbox": {"l": 10, "t": 28, "r": 80, "b": 10},
            },
            llm_client=object(),
        )
    )

    assert captured["schema_name"] == "widget_page_intelligence"
    assert captured["cache_breakpoint_index"] == 1
    assert captured["content"][1]["type"] == "file"
    assert result["field_review_id"] == "field-widget-10-0"
    assert result["suggested_action"] == "preserve_control"
    assert result["confidence"] == "high"


def test_generate_widget_intelligence_for_page_uses_backend_aware_page_input(monkeypatch, tmp_path):
    captured = {}

    async def _fake_request_llm_json_with_response(
        *,
        llm_client,
        content,
        schema_name=None,
        response_schema=None,
        cache_breakpoint_index=None,
        conversation_prefix=None,
    ):
        captured["content"] = content
        captured["schema_name"] = schema_name
        captured["cache_breakpoint_index"] = cache_breakpoint_index
        return (
            {
                "task_type": "widget_page_intelligence",
                "page": 2,
                "summary": "One widget should be preserved.",
                "decisions": [
                    {
                        "field_review_id": "field-widget-10-0",
                        "summary": "The control is interactive.",
                        "confidence": "medium",
                        "suggested_action": "preserve_control",
                        "reason": "The widget is aligned with real form content.",
                    }
                ],
            },
            {"choices": [{"message": {"annotations": []}}]},
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_widgets.request_llm_json_with_response",
        _fake_request_llm_json_with_response,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_widgets.semantic_page_parts",
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
        generate_widget_intelligence_for_page(
            job=_job(tmp_path),
            page_number=2,
            targets=[
                {
                    "field_review_id": "field-widget-10-0",
                    "page": 2,
                    "field_type": "text",
                    "field_name": "f1_01[0]",
                    "accessible_name": "",
                    "bbox": {"l": 10, "t": 28, "r": 80, "b": 10},
                }
            ],
            llm_client=object(),
        )
    )

    assert captured["schema_name"] == "widget_page_intelligence"
    assert captured["cache_breakpoint_index"] == 1
    assert captured["content"][1]["type"] == "file"
    assert result[0]["field_review_id"] == "field-widget-10-0"
    assert result[0]["suggested_action"] == "preserve_control"
    assert result[0]["batch_generated"] is True
