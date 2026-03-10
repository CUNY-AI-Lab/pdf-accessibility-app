import asyncio
from types import SimpleNamespace

from app.services.intelligence_gemini_forms import (
    generate_form_intelligence,
    generate_form_intelligence_for_page,
)
from app.services.semantic_units import SemanticDecision


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
    )


def test_generate_form_intelligence_normalizes_response(monkeypatch, tmp_path):
    captured = {}

    async def _fake_adjudicate(*, job, unit, llm_client):
        captured["unit"] = unit
        return SemanticDecision(
            unit_id="ignored-by-normalizer",
            unit_type="form_field",
            summary="The printed label clearly identifies the field.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_field_label",
            reason="The field sits directly under the visible label.",
            accessible_label="First name and middle initial",
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_forms.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_form_intelligence(
            job=_job(tmp_path),
            target={
                "field_review_id": "field-widget-10-0",
                "page": 1,
                "field_type": "text",
                "field_name": "f1_01[0]",
                "accessible_name": "f1_01[0]",
                "label_quality": "weak",
                "bbox": {"l": 10, "t": 28, "r": 80, "b": 10},
                "nearby_fields": [
                    {
                        "field_review_id": "field-widget-11-0",
                        "field_type": "checkbox",
                        "field_name": "c1_01[0]",
                        "accessible_name": "",
                        "label_quality": "missing",
                    }
                ],
            },
            nearby_blocks=[
                {"review_id": "review-1", "type": "paragraph", "text": "First name and middle initial"},
            ],
            llm_client=object(),
        )
    )

    assert result["task_type"] == "form_intelligence"
    assert result["field_review_id"] == "field-widget-10-0"
    assert result["page"] == 1
    assert result["suggested_action"] == "set_field_label"
    assert result["accessible_label"] == "First name and middle initial"
    assert result["confidence_score"] == 0.9
    assert result["current_accessible_name"] == "f1_01[0]"
    assert result["current_field_name"] == "f1_01[0]"

    unit = captured["unit"]
    assert unit.unit_type == "form_field"
    assert unit.unit_id == "field-widget-10-0"
    assert unit.nearby_context[0]["text"] == "First name and middle initial"
    assert unit.metadata["field_review_target"]["field_name"] == "f1_01[0]"
    assert unit.metadata["nearby_fields"][0]["field_review_id"] == "field-widget-11-0"
    assert unit.current_semantics["label_quality"] == "weak"


def test_generate_form_intelligence_for_page_normalizes_batch_response(monkeypatch, tmp_path):
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
            "task_type": "form_page_intelligence",
            "page": 2,
            "summary": "Visible labels identify the controls.",
            "decisions": [
                {
                    "field_review_id": "field-widget-10-0",
                    "summary": "Visible label matches the first field.",
                    "confidence": "high",
                    "suggested_action": "set_field_label",
                    "reason": "The label is printed directly above the field.",
                    "accessible_label": "First name and middle initial",
                },
                {
                    "field_review_id": "field-widget-11-0",
                    "summary": "Ambiguous checkbox context.",
                    "confidence": "low",
                    "suggested_action": "manual_only",
                    "reason": "The visible context is too ambiguous.",
                },
            ],
        }

    monkeypatch.setattr(
        "app.services.intelligence_gemini_forms.request_llm_json",
        _fake_request_llm_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_forms.page_preview_parts",
        lambda job, page_numbers: [{"type": "image_url", "image_url": {"url": "data:image/png;base64,page"}}],
    )

    result = asyncio.run(
        generate_form_intelligence_for_page(
            job=_job(tmp_path),
            page_number=2,
            targets=[
                {
                    "field_review_id": "field-widget-10-0",
                    "page": 2,
                    "field_type": "text",
                    "field_name": "f1_01[0]",
                    "accessible_name": "",
                    "label_quality": "missing",
                    "bbox": {"l": 10, "t": 28, "r": 80, "b": 10},
                    "nearby_blocks": [
                        {"review_id": "review-1", "type": "paragraph", "text": "First name and middle initial"},
                    ],
                    "nearby_fields": [],
                },
                {
                    "field_review_id": "field-widget-11-0",
                    "page": 2,
                    "field_type": "checkbox",
                    "field_name": "c1_01[0]",
                    "accessible_name": "",
                    "label_quality": "missing",
                    "bbox": {"l": 90, "t": 28, "r": 110, "b": 10},
                    "nearby_blocks": [],
                    "nearby_fields": [],
                },
            ],
            llm_client=object(),
        )
    )

    assert captured["schema_name"] == "form_page_intelligence"
    assert captured["cache_breakpoint_index"] == 1
    assert len(result) == 2
    assert result[0]["field_review_id"] == "field-widget-10-0"
    assert result[0]["suggested_action"] == "set_field_label"
    assert result[0]["accessible_label"] == "First name and middle initial"
    assert result[0]["batch_generated"] is True
    assert result[0]["confidence_score"] == 0.9
    assert result[1]["field_review_id"] == "field-widget-11-0"
    assert result[1]["suggested_action"] == "manual_only"
    assert result[1]["confidence_score"] == 0.4
