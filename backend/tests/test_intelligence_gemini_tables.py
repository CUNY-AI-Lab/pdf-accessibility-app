import asyncio
from types import SimpleNamespace

from app.services.intelligence_gemini_tables import generate_table_intelligence
from app.services.semantic_units import SemanticDecision


def _job(tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
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
