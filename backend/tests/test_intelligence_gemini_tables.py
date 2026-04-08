import asyncio
from types import SimpleNamespace

import pikepdf

from app.services.intelligence_gemini_tables import (
    generate_table_intelligence,
    generate_table_intelligence_for_page,
)
from app.services.semantic_units import SemanticDecision


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


def test_generate_table_intelligence_returns_normalized_update(monkeypatch, tmp_path):
    captured = {}

    async def _fake_adjudicate(*, job, unit, llm_client):
        captured["unit"] = unit
        return SemanticDecision(
            unit_id="review-7",
            unit_type="table",
            summary="The first row and first column act as headers.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_table_headers",
            reason="The table is regular and the labels are visually clear.",
            header_rows=[0],
            row_header_columns=[0],
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_tables.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_table_intelligence(
            job=_job(tmp_path),
            target={
                "table_review_id": "review-7",
                "page": 1,
                "bbox": {"l": 10, "t": 20, "r": 100, "b": 120},
                "cells": [{"row": 0, "col": 0, "text": "Header"}],
            },
            page_structure_fragments=[{"page": 1, "type": "paragraph", "text": "Nearby caption"}],
            llm_client=object(),
            previous_intelligence={"summary": "Previous", "suggested_action": "manual_only"},
        )
    )

    assert result == {
        "task_type": "table_intelligence",
        "summary": "The first row and first column act as headers.",
        "confidence": "high",
        "confidence_score": 0.9,
        "suggested_action": "set_table_headers",
        "reason": "The table is regular and the labels are visually clear.",
        "table_review_id": "review-7",
        "page": 1,
        "header_rows": [0],
        "row_header_columns": [0],
    }
    unit = captured["unit"]
    assert unit.unit_type == "table"
    assert unit.unit_id == "review-7"
    assert unit.structure_context[0]["text"] == "Nearby caption"
    assert unit.metadata["table_review_target"]["cells"][0]["text"] == "Header"
    assert unit.metadata["previous_intelligence"] == {"summary": "Previous", "suggested_action": "manual_only"}


def test_generate_table_intelligence_for_page_uses_pdf_file_input(monkeypatch, tmp_path):
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
                "task_type": "table_page_intelligence",
                "page": 2,
                "summary": "Headers are clear enough from the visible page.",
                "decisions": [
                    {
                        "table_review_id": "review-7",
                        "summary": "First row and first column act as headers.",
                        "confidence": "high",
                        "suggested_action": "set_table_headers",
                        "reason": "The visible grid and labels are regular.",
                        "header_rows": [0],
                        "row_header_columns": [0],
                    }
                ],
            },
            {"choices": [{"message": {"annotations": []}}]},
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_tables.request_llm_json_with_response",
        _fake_request_llm_json_with_response,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_tables.pdf_file_parts",
        lambda job, page_numbers, filename=None: [{"type": "file", "file": {"filename": filename or "sample.pdf", "file_data": "data:application/pdf;base64,page"}}],
    )

    result = asyncio.run(
        generate_table_intelligence_for_page(
            job=_job(tmp_path),
            page_number=2,
            targets=[
                {
                    "table_review_id": "review-7",
                    "page": 2,
                    "bbox": {"l": 10, "t": 20, "r": 100, "b": 120},
                    "cells": [{"row": 0, "col": 0, "text": "Header"}],
                    "num_rows": 2,
                    "num_cols": 2,
                    "header_rows": [],
                    "row_header_columns": [],
                    "risk_score": 0.8,
                    "risk_reasons": ["missing headers"],
                    "text_excerpt": "Header data",
                }
            ],
            page_structure_fragments=[{"page": 2, "type": "paragraph", "text": "Nearby caption"}],
            llm_client=object(),
        )
    )

    assert captured["schema_name"] == "table_page_intelligence"
    assert captured["cache_breakpoint_index"] == 1
    assert captured["content"][1]["type"] == "file"
    assert result[0]["table_review_id"] == "review-7"
    assert result[0]["suggested_action"] == "set_table_headers"
    assert result[0]["header_rows"] == [0]
    assert result[0]["row_header_columns"] == [0]
    assert result[0]["batch_generated"] is True
