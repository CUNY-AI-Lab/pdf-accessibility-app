import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import review_suggestions
from app.services.review_suggestions import (
    _extract_json_object,
    _font_task_payload,
    _reading_order_task_payload,
    _table_target_payload,
    _table_task_payload,
    generate_review_suggestion,
    select_auto_font_review_resolution,
    select_auto_font_map_override,
)


def _job(tmp_path, *, structure: dict | None = None):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test\n")
    return SimpleNamespace(
        original_filename="sample.pdf",
        input_path=str(pdf_path),
        output_path=str(pdf_path),
        structure_json=json.dumps(structure or {}),
    )


def _task(task_type: str, *, metadata: dict | None = None):
    return SimpleNamespace(
        task_type=task_type,
        title=f"{task_type} task",
        detail=f"{task_type} detail",
        severity="high",
        source="fidelity",
        metadata_json=json.dumps(metadata or {}),
    )


class _FakeLlmClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.model = "google/gemini-3-flash-preview"
        self.calls: list[dict] = []

    async def chat_completion(self, messages, **kwargs):
        if isinstance(self.payload, list):
            if not self.payload:
                raise AssertionError("No fake LLM payloads left")
            payload = self.payload.pop(0)
        else:
            payload = self.payload
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(payload),
                    }
                }
            ]
        }


def test_extract_json_object_accepts_fenced_json():
    parsed = _extract_json_object(
        """```json
        {"summary":"ok","confidence":"high"}
        ```"""
    )

    assert parsed == {"summary": "ok", "confidence": "high"}


def test_job_pdf_path_falls_back_to_input_when_output_missing(tmp_path):
    job = _job(tmp_path)
    missing_output = tmp_path / "missing-output.pdf"
    job.output_path = str(missing_output)

    pdf_path = review_suggestions._job_pdf_path(job)

    assert pdf_path == Path(job.input_path)


def test_font_task_payload_uses_review_targets_and_page_structure_context(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        review_suggestions,
        "render_target_preview_png_data_url",
        lambda pdf_path, context_path: f"data:image/png;base64,target-{hash(context_path) % 10}",
    )

    prompt_text, content = _font_task_payload(
        _job(
            tmp_path,
            structure={
                "elements": [
                    {"type": "list_item", "page": 1, "text": "Arrow marker precedes this list entry in the source."},
                    {"type": "paragraph", "page": 1, "text": "The same marker repeats before multiple entries."},
                ]
            },
        ),
        _task(
            "font_text_fidelity",
            metadata={
                "pages_to_check": [2, 5],
                "fonts_to_check": ["MathematicalPi-Six"],
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "MathematicalPi-Six",
                        "operator_index": 132,
                        "context_path": "root/document[0]/pages[1]/contentStream[0]/operators[132]/usedGlyphs[0](Font Font 1 0 0 0 true)",
                    }
                ],
            },
        ),
    )

    assert '"pages_to_check": [\n    2,\n    5\n  ]' in prompt_text
    assert "MathematicalPi-Six" in prompt_text
    assert '"target_previews": [' in prompt_text
    assert '"page_structure_fragments": [' in prompt_text
    assert "Arrow marker precedes this list entry in the source." in prompt_text
    assert len(content) == 4
    assert content[1]["image_url"]["url"] == "data:image/png;base64,page-2"
    assert content[2]["image_url"]["url"] == "data:image/png;base64,page-5"
    assert content[3]["image_url"]["url"].startswith("data:image/png;base64,target-")


def test_reading_order_task_payload_collects_structure_fragments(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        review_suggestions,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:block-{page_number}",
    )

    prompt_text, content = _reading_order_task_payload(
        _job(
            tmp_path,
            structure={
                "elements": [
                    {"type": "heading", "page": 0, "text": "Library AI Discovery Guide"},
                    {
                        "type": "paragraph",
                        "page": 0,
                        "text": "D a t a  B o o k",
                        "bbox": {"l": 72, "t": 700, "r": 250, "b": 660},
                    },
                    {"type": "paragraph", "page": 1, "text": "A sidebar note appears before the main content."},
                ]
            },
        ),
        _task("reading_order", metadata={"hit_rate": 0.52, "order_rate": 0.61}),
    )

    assert '"pages_to_check": [\n    1,\n    2\n  ]' in prompt_text
    assert "Library AI Discovery Guide" in prompt_text
    assert "A sidebar note appears before the main content." in prompt_text
    assert '"page_blocks": [' in prompt_text
    assert '"suspicious_text_blocks": [' in prompt_text
    assert '"D a t a B o o k"' in prompt_text
    assert '"review_id": "review-0"' in prompt_text
    assert '"current_order": 1' in prompt_text
    assert len(content) == 4
    assert content[1]["image_url"]["url"] == "data:image/png;base64,page-1"
    assert content[2]["image_url"]["url"] == "data:image/png;base64,page-2"
    assert content[3]["image_url"]["url"] == "data:image/png;base64:block-1"


def test_generate_review_suggestion_supports_reading_order(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )

    fake_llm = _FakeLlmClient(
        {
            "task_type": "reading_order",
            "summary": "Sidebar likely interrupts the main text flow.",
            "confidence": "medium",
            "suggested_action": "reorder_review",
            "reason": "The sampled blocks suggest a sidebar before the body text.",
            "review_focus": [{"page": 1, "recommended_reviewer_action": "check sidebar order"}],
            "reviewer_checklist": ["Verify order with NVDA"],
            "readable_text_hints": [
                {
                    "page": 1,
                    "review_id": "review-1",
                    "extracted_text": "D a t a  B o o k",
                    "readable_text_hint": "Data Book",
                    "issue_type": "spacing_only",
                    "confidence": "high",
                    "should_block_accessibility": False,
                    "reason": "The page image shows a title with broken extraction spacing.",
                }
            ],
        }
    )

    suggestion = asyncio.run(
        generate_review_suggestion(
            job=_job(
                tmp_path,
                structure={
                    "elements": [
                        {"type": "heading", "page": 0, "text": "Guide heading for review"},
                        {"type": "paragraph", "page": 0, "text": "D a t a  B o o k"},
                    ]
                },
            ),
            task=_task("reading_order", metadata={"hit_rate": 0.44, "order_rate": 0.62}),
            llm_client=fake_llm,
        )
    )

    assert suggestion["task_type"] == "reading_order"
    assert suggestion["suggested_action"] == "reorder_review"
    assert suggestion["readable_text_hints"][0]["readable_text_hint"] == "Data Book"
    assert suggestion["model"] == "google/gemini-3-flash-preview"
    assert fake_llm.calls
    assert fake_llm.calls[0]["kwargs"]["temperature"] == 0


def test_generate_review_suggestion_keeps_font_actualtext_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )

    fake_llm = _FakeLlmClient(
        {
            "task_type": "font_text_fidelity",
            "summary": "Single symbol likely needs manual ActualText.",
            "confidence": "medium",
            "suggested_action": "actualtext_candidate",
            "reason": "The remaining font issue is localized to one operator.",
            "review_focus": [
                {
                    "page": 2,
                    "font": "MathematicalPi-Six",
                    "operator_index": 132,
                    "recommended_reviewer_action": "compare the visible symbol and apply ActualText if it matches",
                }
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "MathematicalPi-Six",
                    "proposed_actualtext": "bullet",
                    "confidence": "medium",
                    "reason": "Appears to be a single bullet-like glyph.",
                }
            ],
            "reviewer_checklist": ["Verify the symbol visually before applying."],
        }
    )

    suggestion = asyncio.run(
        generate_review_suggestion(
            job=_job(tmp_path),
            task=_task(
                "font_text_fidelity",
                metadata={
                    "pages_to_check": [2],
                    "fonts_to_check": ["MathematicalPi-Six"],
                    "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                    "font_review_targets": [
                        {"page": 2, "font": "MathematicalPi-Six", "operator_index": 132}
                    ],
                },
            ),
            llm_client=fake_llm,
        )
    )

    candidates = suggestion.get("actualtext_candidates")
    assert isinstance(candidates, list)
    assert candidates[0]["operator_index"] == 132
    assert candidates[0]["proposed_actualtext"] == "bullet"
    assert fake_llm.calls[0]["kwargs"]["temperature"] == 0


def test_table_task_payload_collects_table_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        review_suggestions,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:table-{page_number}",
    )

    prompt_text, content = _table_task_payload(
        _job(
            tmp_path,
            structure={
                "elements": [
                    {
                        "type": "table",
                        "page": 0,
                        "text": "Enrollment by program and semester",
                        "bbox": {"l": 72, "t": 700, "r": 400, "b": 500},
                        "num_rows": 3,
                        "num_cols": 2,
                        "cells": [
                            {"row": 0, "col": 0, "text": "Program", "column_header": True, "row_header": False},
                            {"row": 0, "col": 1, "text": "Students", "column_header": True, "row_header": False},
                            {"row": 1, "col": 0, "text": "History", "column_header": False, "row_header": True},
                            {"row": 1, "col": 1, "text": "45", "column_header": False, "row_header": False},
                        ],
                    }
                ]
            },
        ),
        _task(
            "table_semantics",
            metadata={"detected_tables": 2, "tagged_tables": 1},
        ),
    )

    assert '"table_review_targets": [' in prompt_text
    assert '"table_review_id": "review-0"' in prompt_text
    assert '"header_rows": [' in prompt_text
    assert '"row_header_columns": [' in prompt_text
    assert "Enrollment by program and semester" in prompt_text
    assert len(content) == 3
    assert content[1]["image_url"]["url"] == "data:image/png;base64,page-1"
    assert content[2]["image_url"]["url"] == "data:image/png;base64:table-1"


def test_table_target_payload_focuses_one_table_with_accessibility_goal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        review_suggestions,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:table-{page_number}",
    )

    job = _job(
        tmp_path,
        structure={
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "text": "Enrollment by program and semester",
                    "bbox": {"l": 72, "t": 700, "r": 400, "b": 500},
                    "num_rows": 3,
                    "num_cols": 2,
                    "cells": [
                        {"row": 0, "col": 0, "text": "Program", "column_header": True},
                        {"row": 0, "col": 1, "text": "Students", "column_header": True},
                        {"row": 1, "col": 0, "text": "History", "row_header": True},
                        {"row": 1, "col": 1, "text": "45"},
                    ],
                }
            ]
        },
    )
    target = review_suggestions._table_targets_for_review(
        job,
        {"table_review_targets": [{"table_review_id": "review-0", "page": 1}]},
    )[0]

    prompt_text, content = _table_target_payload(
        job,
        _task("table_semantics", metadata={"detected_tables": 1, "tagged_tables": 1}),
        target,
    )

    assert '"accessibility_goal": "Identify header rows and row-header columns only when they improve how screen readers and other assistive technologies understand this table."' in prompt_text
    assert '"table_review_target": {' in prompt_text
    assert '"table_review_id": "review-0"' in prompt_text
    assert "Preserve the visible meaning and relationships in the source table." in prompt_text
    assert len(content) == 3
    assert content[1]["image_url"]["url"] == "data:image/png;base64,page-1"
    assert content[2]["image_url"]["url"] == "data:image/png;base64:table-1"


def test_generate_review_suggestion_supports_table_semantics(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "_render_page_image",
        lambda pdf_path, page_number: f"data:image/png;base64,page-{page_number}",
    )
    monkeypatch.setattr(
        review_suggestions,
        "render_bbox_preview_png_data_url",
        lambda pdf_path, page_number, bbox: f"data:image/png;base64:table-{page_number}",
    )

    fake_llm = _FakeLlmClient(
        [
            {
                "task_type": "table_semantics",
                "summary": "The first row and first column look like headers.",
                "confidence": "high",
                "suggested_action": "set_table_headers",
                "reason": "The crop shows a regular matrix with a header row and row labels.",
                "proposed_table_updates": [
                    {
                        "page": 1,
                        "table_review_id": "review-0",
                        "header_rows": [0],
                        "row_header_columns": [0],
                        "reason": "Top row contains column labels and first column contains row labels.",
                    }
                ],
                "reviewer_checklist": ["Verify headers with a screen reader."],
            },
            {
                "task_type": "table_semantics",
                "summary": "The second table needs manual interpretation.",
                "confidence": "medium",
                "suggested_action": "manual_only",
                "reason": "Merged headers make simple row and column flags insufficient.",
                "proposed_table_updates": [],
                "reviewer_checklist": ["Check merged header groupings manually."],
            },
        ]
    )

    suggestion = asyncio.run(
        generate_review_suggestion(
            job=_job(
                tmp_path,
                structure={
                    "elements": [
                        {
                            "type": "table",
                            "page": 0,
                            "text": "Enrollment by program and semester",
                            "bbox": {"l": 72, "t": 700, "r": 400, "b": 500},
                            "num_rows": 3,
                            "num_cols": 2,
                            "cells": [
                                {"row": 0, "col": 0, "text": "Program"},
                                {"row": 0, "col": 1, "text": "Students"},
                                {"row": 1, "col": 0, "text": "History"},
                                {"row": 1, "col": 1, "text": "45"},
                            ],
                        },
                        {
                            "type": "table",
                            "page": 0,
                            "text": "Enrollment by college and year",
                            "bbox": {"l": 72, "t": 460, "r": 400, "b": 260},
                            "num_rows": 4,
                            "num_cols": 3,
                            "cells": [
                                {"row": 0, "col": 0, "text": ""},
                                {"row": 0, "col": 1, "text": "2024", "col_span": 2},
                                {"row": 1, "col": 0, "text": "College"},
                                {"row": 1, "col": 1, "text": "UG"},
                                {"row": 1, "col": 2, "text": "Grad"},
                            ],
                        },
                    ]
                },
            ),
            task=_task(
                "table_semantics",
                metadata={
                    "detected_tables": 2,
                    "tagged_tables": 0,
                    "table_review_targets": [
                        {"table_review_id": "review-0", "page": 1},
                        {"table_review_id": "review-1", "page": 1},
                    ],
                },
            ),
            llm_client=fake_llm,
        )
    )

    assert suggestion["task_type"] == "table_semantics"
    assert suggestion["suggested_action"] == "set_table_headers"
    assert suggestion["confidence"] == "medium"
    assert len(suggestion["proposed_table_updates"]) == 1
    assert "manual review" in suggestion["summary"].lower()
    assert fake_llm.calls
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[0]["kwargs"]["temperature"] == 0


def test_generate_review_suggestion_rejects_unsupported_task(tmp_path):
    fake_llm = _FakeLlmClient({"summary": "n/a"})

    with pytest.raises(ValueError):
        asyncio.run(
            generate_review_suggestion(
                job=_job(tmp_path),
                task=_task("annotation_description"),
                llm_client=fake_llm,
            )
        )


def test_select_auto_font_map_override_accepts_high_confidence_single_glyph(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_map_override(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    },
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 194,
                        "context_path": "ctx-2",
                    },
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "font_map_candidate",
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                },
            ],
        },
    )

    assert selected == {
        "page_number": 2,
        "operator_index": 132,
        "unicode_text": "►",
        "font": "ExampleSymbolFont",
        "font_base_name": "ExampleSymbolFont",
        "font_code_hex": "01",
        "target_count": 2,
    }


def test_select_auto_font_map_override_rejects_divergent_or_low_confidence_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    task = _task(
        "font_text_fidelity",
        metadata={
            "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
            "font_review_targets": [
                {
                    "page": 2,
                    "font": "ExampleSymbolFont",
                    "operator_index": 132,
                    "context_path": "ctx-1",
                }
            ],
        },
    )

    low_confidence = select_auto_font_map_override(
        job=_job(tmp_path),
        task=task,
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "medium",
            "suggested_action": "font_map_candidate",
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "medium",
                }
            ],
        },
    )
    assert low_confidence is None

    divergent = select_auto_font_map_override(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    },
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 194,
                        "context_path": "ctx-2",
                    },
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "font_map_candidate",
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "•",
                    "confidence": "high",
                },
            ],
        },
    )
    assert divergent is None


def test_select_auto_font_map_override_accepts_actualtext_action_when_candidates_are_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_map_override(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    }
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "actualtext_candidate",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": False,
                }
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                }
            ],
        },
    )

    assert selected == {
        "page_number": 2,
        "operator_index": 132,
        "unicode_text": "►",
        "font": "ExampleSymbolFont",
        "font_base_name": "ExampleSymbolFont",
        "font_code_hex": "01",
        "target_count": 1,
    }


def test_select_auto_font_map_override_rejects_decorative_actualtext_action(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_map_override(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    }
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "actualtext_candidate",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": True,
                }
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                }
            ],
        },
    )

    assert selected is None


def test_select_auto_font_review_resolution_accepts_decorative_artifact_action(monkeypatch, tmp_path):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_review_resolution(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    },
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 194,
                        "context_path": "ctx-2",
                    },
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "artifact_if_decorative",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": True,
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "is_likely_decorative": True,
                },
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "high",
                },
            ],
        },
    )

    assert selected == {
        "resolution_type": "artifact",
        "font": "ExampleSymbolFont",
        "font_base_name": "ExampleSymbolFont",
        "font_code_hex": "01",
        "unicode_text": "►",
        "target_count": 2,
        "targets": [
            {"page_number": 2, "operator_index": 132, "context_path": "ctx-1"},
            {"page_number": 2, "operator_index": 194, "context_path": "ctx-2"},
        ],
    }


def test_select_auto_font_review_resolution_accepts_decorative_artifact_without_actualtext_candidates(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_review_resolution(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    }
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "artifact_if_decorative",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": True,
                }
            ],
            "actualtext_candidates": [],
        },
    )

    assert selected == {
        "resolution_type": "artifact",
        "font": "ExampleSymbolFont",
        "font_base_name": "ExampleSymbolFont",
        "font_code_hex": "01",
        "unicode_text": "",
        "target_count": 1,
        "targets": [
            {"page_number": 2, "operator_index": 132, "context_path": "ctx-1"},
        ],
    }


def test_select_auto_font_review_resolution_ignores_low_confidence_actualtext_candidates_for_artifact(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_review_resolution(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    },
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 194,
                        "context_path": "ctx-2",
                    },
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "artifact_if_decorative",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": True,
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "is_likely_decorative": True,
                },
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "medium",
                },
                {
                    "page": 2,
                    "operator_index": 194,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "►",
                    "confidence": "medium",
                },
            ],
        },
    )

    assert selected is not None
    assert selected["resolution_type"] == "artifact"
    assert selected["unicode_text"] == ""


def test_select_auto_font_review_resolution_uses_visible_hint_for_artifact_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        review_suggestions,
        "inspect_context_font_target",
        lambda pdf_path, context_path: {
            "font_code": 1,
            "font_code_hex": "01",
            "font_base_name": "ExampleSymbolFont",
            "target_operator": "Tj",
        },
    )

    selected = select_auto_font_review_resolution(
        job=_job(tmp_path),
        task=_task(
            "font_text_fidelity",
            metadata={
                "font_rule_ids": ["ISO 14289-1:2014-7.21.7-1"],
                "font_review_targets": [
                    {
                        "page": 2,
                        "font": "ExampleSymbolFont",
                        "operator_index": 132,
                        "context_path": "ctx-1",
                    }
                ],
            },
        ),
        suggestion={
            "task_type": "font_text_fidelity",
            "confidence": "high",
            "suggested_action": "artifact_if_decorative",
            "review_focus": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "is_likely_decorative": True,
                    "visible_text_hypothesis": "right-pointing triangle",
                }
            ],
            "actualtext_candidates": [
                {
                    "page": 2,
                    "operator_index": 132,
                    "font": "ExampleSymbolFont",
                    "proposed_actualtext": "",
                    "confidence": "high",
                }
            ],
        },
    )

    assert selected is not None
    assert selected["resolution_type"] == "artifact"
    assert selected["unicode_text"] == "►"
