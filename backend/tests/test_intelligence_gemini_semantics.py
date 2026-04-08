import asyncio
import json
from types import SimpleNamespace

import pikepdf
import pytest

from app.services import intelligence_gemini_semantics
from app.services.intelligence_gemini_semantics import (
    adjudicate_semantic_unit,
    adjudicate_semantic_units,
)
from app.services.semantic_units import SemanticUnit


@pytest.fixture(autouse=True)
def _disable_direct_gemini_by_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.intelligence_llm_utils.direct_gemini_pdf_enabled",
        lambda: False,
    )


class _FakeLlmClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def chat_completion(self, messages, **kwargs):
        payload = self.payloads.pop(0)
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}


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


def test_adjudicate_semantic_unit_normalizes_text_block(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intelligence_gemini_semantics,
        "pdf_file_parts",
        lambda job, page_numbers, filename=None: [
            {
                "type": "file",
                "file": {"filename": filename or "sample.pdf", "file_data": f"data:application/pdf;base64:page-{next(iter(page_numbers), 1)}"},
            }
        ],
    )
    monkeypatch.setattr(
        intelligence_gemini_semantics,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:bbox-{page_number}",
    )
    llm = _FakeLlmClient(
        [
            {
                "task_type": "semantic_unit_adjudication",
                "summary": "Spacing is broken but the title is clear.",
                "confidence": "high",
                "unit_id": "review-1",
                "unit_type": "text_block",
                "suggested_action": "set_resolved_text",
                "reason": "OCR matches the visible title.",
                "chosen_source": "ocr",
                "resolved_text": "Data Book",
                "issue_type": "spacing_only",
                "should_block_accessibility": True,
            }
        ]
    )

    unit = SemanticUnit(
        unit_id="review-1",
        unit_type="text_block",
        page=1,
        accessibility_goal="Infer what assistive technology should hear.",
        bbox={"l": 10, "t": 20, "r": 50, "b": 40},
        native_text_candidate="D a t a  B o o k",
        ocr_text_candidate="Data Book",
        metadata={
            "signals": ["letters separated by spaces"],
            "previous_intelligence": {
                "summary": "Previous suggestion",
                "suggested_action": "set_resolved_text",
                "reason": "Old reason",
                "resolved_text": "Data Book",
            },
        },
    )
    decision = asyncio.run(adjudicate_semantic_unit(job=_job(tmp_path), unit=unit, llm_client=llm))

    assert decision.unit_id == "review-1"
    assert decision.unit_type == "text_block"
    assert decision.suggested_action == "set_resolved_text"
    assert decision.resolved_text == "Data Book"
    assert decision.chosen_source == "ocr"
    assert decision.issue_type == "spacing_only"
    assert llm.calls[0]["kwargs"]["response_format"]["type"] == "json_schema"
    prompt = llm.calls[0]["messages"][0]["content"][0]["text"]
    context = next(
        item["text"]
        for item in llm.calls[0]["messages"][0]["content"]
        if item.get("type") == "text" and item.get("text", "").startswith("Context JSON:\n")
    )
    assert "semantic adjudication assistant" in prompt
    assert '"unit_type": "text_block"' in context
    assert '"native_text_candidate": "D a t a  B o o k"' in context
    assert '"ocr_text_candidate": "Data Book"' in context
    assert '"previous_intelligence": {' in context
    assert '"summary": "Previous suggestion"' in context
    cached_part = next(
        item
        for item in llm.calls[0]["messages"][0]["content"]
        if item.get("cache_control") == {"type": "ephemeral"}
    )
    assert cached_part["type"] in {"file", "image_url"}
    assert llm.calls[0]["messages"][0]["content"][1]["type"] == "file"


def test_adjudicate_semantic_units_preserves_order(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intelligence_gemini_semantics,
        "pdf_file_parts",
        lambda job, page_numbers, filename=None: [
            {
                "type": "file",
                "file": {"filename": filename or "sample.pdf", "file_data": f"data:application/pdf;base64:page-{next(iter(page_numbers), 1)}"},
            }
        ],
    )
    llm = _FakeLlmClient(
        [
            {
                "summary": "Field label is clear.",
                "confidence": "high",
                "suggested_action": "set_field_label",
                "reason": "Visible label is adjacent.",
                "accessible_label": "First name",
            },
            {
                "summary": "Headers are already acceptable.",
                "confidence": "medium",
                "suggested_action": "confirm_current_headers",
                "reason": "Current headers provide a usable reading.",
                "header_rows": [0],
                "row_header_columns": [0],
            },
        ]
    )
    units = [
        SemanticUnit(
            unit_id="field-1",
            unit_type="form_field",
            page=1,
            accessibility_goal="Label the field.",
        ),
        SemanticUnit(
            unit_id="table-1",
            unit_type="table",
            page=2,
            accessibility_goal="Interpret the table.",
        ),
    ]

    decisions = asyncio.run(
        adjudicate_semantic_units(job=_job(tmp_path), units=units, llm_client=llm)
    )

    assert [decision.unit_id for decision in decisions] == ["field-1", "table-1"]
    assert decisions[0].accessible_label == "First name"
    assert decisions[1].header_rows == [0]


def test_adjudicate_semantic_unit_repairs_missing_required_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intelligence_gemini_semantics,
        "pdf_file_parts",
        lambda job, page_numbers, filename=None: [
            {
                "type": "file",
                "file": {"filename": filename or "sample.pdf", "file_data": f"data:application/pdf;base64:page-{next(iter(page_numbers), 1)}"},
            }
        ],
    )
    llm = _FakeLlmClient(
        [
            {
                "task_type": "semantic_unit_adjudication",
                "summary": "The field label is visible next to the box.",
                "confidence": "high",
                "unit_id": "field-1",
                "unit_type": "form_field",
                "suggested_action": "set_field_label",
                "reason": "The visible label is clear.",
                "accessible_label": "",
            },
            {
                "task_type": "semantic_unit_adjudication",
                "summary": "The field label is visible next to the box.",
                "confidence": "high",
                "unit_id": "field-1",
                "unit_type": "form_field",
                "suggested_action": "set_field_label",
                "reason": "The visible label is clear.",
                "accessible_label": "Line 4b QCD",
            },
        ]
    )

    unit = SemanticUnit(
        unit_id="field-1",
        unit_type="form_field",
        page=1,
        accessibility_goal="Choose the accessible field label that assistive technology should announce.",
        bbox={"l": 10, "t": 20, "r": 50, "b": 40},
        current_semantics={
            "field_name": "c1_37[0]",
            "accessible_label": "",
            "field_type": "checkbox",
        },
    )
    decision = asyncio.run(adjudicate_semantic_unit(job=_job(tmp_path), unit=unit, llm_client=llm))

    assert decision.suggested_action == "set_field_label"
    assert decision.accessible_label == "Line 4b QCD"
    assert len(llm.calls) == 2
    repair_prompt = llm.calls[1]["messages"][0]["content"][1]["text"]
    assert "populate `accessible_label`" in repair_prompt

def test_adjudicate_semantic_unit_allows_cross_type_reclassification_for_table(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        intelligence_gemini_semantics,
        "pdf_file_parts",
        lambda job, page_numbers, filename=None: [
            {
                "type": "file",
                "file": {"filename": filename or "sample.pdf", "file_data": f"data:application/pdf;base64:page-{next(iter(page_numbers), 1)}"},
            }
        ],
    )
    llm = _FakeLlmClient(
        [
            {
                "task_type": "semantic_unit_adjudication",
                "summary": "This is a hierarchical org chart, not a data table.",
                "confidence": "high",
                "unit_id": "table-1",
                "unit_type": "table",
                "suggested_action": "reclassify_region",
                "reason": "The boxes show reporting relationships instead of row and column headers.",
                "resolved_kind": "org_chart",
            }
        ]
    )

    unit = SemanticUnit(
        unit_id="table-1",
        unit_type="table",
        page=1,
        accessibility_goal="Interpret the table faithfully for assistive technology.",
        current_semantics={"header_rows": [0], "row_header_columns": [0]},
    )
    decision = asyncio.run(adjudicate_semantic_unit(job=_job(tmp_path), unit=unit, llm_client=llm))

    assert decision.suggested_action == "reclassify_region"
    assert decision.resolved_kind == "org_chart"
