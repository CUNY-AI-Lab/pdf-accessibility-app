import asyncio
import json
from types import SimpleNamespace

from app.services import intelligence_gemini_tables
from app.services.intelligence_gemini_tables import generate_table_intelligence


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


def test_generate_table_intelligence_returns_normalized_update(monkeypatch, tmp_path):
    monkeypatch.setattr(
        intelligence_gemini_tables,
        "render_page_png_data_url",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        intelligence_gemini_tables,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:table-{page_number}",
    )

    fake_llm = _FakeLlmClient(
        {
            "task_type": "table_intelligence",
            "summary": "The first row and first column act as headers.",
            "confidence": "high",
            "suggested_action": "set_table_headers",
            "reason": "The table is regular and the labels are visually clear.",
            "table_review_id": "review-7",
            "page": 1,
            "header_rows": [0],
            "row_header_columns": [0],
        }
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
            llm_client=fake_llm,
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
    assert fake_llm.calls
