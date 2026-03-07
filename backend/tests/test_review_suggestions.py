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
    generate_review_suggestion,
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

    prompt_text, content = _reading_order_task_payload(
        _job(
            tmp_path,
            structure={
                "elements": [
                    {"type": "heading", "page": 0, "text": "Library AI Discovery Guide"},
                    {"type": "paragraph", "page": 0, "text": "This section explains the search interface in detail."},
                    {"type": "paragraph", "page": 1, "text": "A sidebar note appears before the main content."},
                ]
            },
        ),
        _task("reading_order", metadata={"hit_rate": 0.52, "order_rate": 0.61}),
    )

    assert '"pages_to_check": [\n    1,\n    2\n  ]' in prompt_text
    assert "Library AI Discovery Guide" in prompt_text
    assert "A sidebar note appears before the main content." in prompt_text
    assert len(content) == 3
    assert content[1]["image_url"]["url"] == "data:image/png;base64,page-1"
    assert content[2]["image_url"]["url"] == "data:image/png;base64,page-2"


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
        }
    )

    suggestion = asyncio.run(
        generate_review_suggestion(
            job=_job(
                tmp_path,
                structure={
                    "elements": [
                        {"type": "heading", "page": 0, "text": "Guide heading for review"},
                        {"type": "paragraph", "page": 0, "text": "Sidebar copy appears before the body text."},
                    ]
                },
            ),
            task=_task("reading_order", metadata={"hit_rate": 0.44, "order_rate": 0.62}),
            llm_client=fake_llm,
        )
    )

    assert suggestion["task_type"] == "reading_order"
    assert suggestion["suggested_action"] == "reorder_review"
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


def test_generate_review_suggestion_rejects_unsupported_task(tmp_path):
    fake_llm = _FakeLlmClient({"summary": "n/a"})

    with pytest.raises(ValueError):
        asyncio.run(
            generate_review_suggestion(
                job=_job(tmp_path),
                task=_task("table_semantics"),
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
