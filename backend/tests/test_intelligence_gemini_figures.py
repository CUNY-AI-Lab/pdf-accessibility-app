import asyncio
from pathlib import Path

from app.pipeline.alt_text import generate_alt_text
from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini_figures import (
    generate_figure_intelligence,
    generate_figures_intelligence,
)
from app.services.semantic_units import SemanticDecision


def test_generate_figure_intelligence_normalizes_alt_text(monkeypatch, tmp_path):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"fake-image")
    captured = {}

    async def _fake_adjudicate(*, job, unit, llm_client):
        captured["unit"] = unit
        assert job.original_filename == "doc.pdf"
        return SemanticDecision(
            unit_id="figure-2",
            unit_type="figure",
            summary="Chart needs short alt text.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_alt_text",
            reason="The chart title and bars are visible.",
            alt_text="Bar chart showing yearly enrollment increasing from 2021 to 2024.",
            is_decorative=False,
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_figure_intelligence(
            figure=FigureInfo(index=2, path=image_path, caption="Enrollment by year", page=0),
            llm_client=object(),
            original_filename="doc.pdf",
        )
    )

    assert result["task_type"] == "figure_intelligence"
    assert result["figure_index"] == 2
    assert result["suggested_action"] == "set_alt_text"
    assert result["alt_text"].startswith("Bar chart")
    assert result["resolved_kind"] is None
    assert result["is_decorative"] is False
    unit = captured["unit"]
    assert unit.unit_type == "figure"
    assert unit.unit_id == "figure-2"
    assert unit.bbox is None
    assert unit.metadata["figure_index"] == 2
    assert unit.metadata["extra_image_data_urls"]


def test_generate_figure_intelligence_fails_soft_to_manual_only(monkeypatch, tmp_path):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"fake-image")

    async def _boom(*, job, unit, llm_client):
        raise ValueError("bad json")

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.adjudicate_semantic_unit",
        _boom,
    )

    result = asyncio.run(
        generate_figure_intelligence(
            figure=FigureInfo(index=7, path=image_path, caption="A warning icon", page=0),
            llm_client=object(),
            original_filename="doc.pdf",
        )
    )

    assert result["task_type"] == "figure_intelligence"
    assert result["figure_index"] == 7
    assert result["suggested_action"] == "manual_only"
    assert result["alt_text"] == ""
    assert result["resolved_kind"] is None
    assert result["is_decorative"] is False


def test_generate_figure_intelligence_can_reclassify_nonfigure_region(monkeypatch, tmp_path):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"fake-image")
    captured = {}

    async def _fake_adjudicate(*, job, unit, llm_client):
        captured["unit"] = unit
        return SemanticDecision(
            unit_id="figure-3",
            unit_type="figure",
            summary="This crop is a table region, not a standalone figure.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="reclassify_region",
            reason="The crop contains tabular return-code rows with headers.",
            resolved_kind="table",
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_figure_intelligence(
            figure=FigureInfo(
                index=3,
                path=image_path,
                caption=None,
                page=14,
                bbox={"l": 10, "b": 20, "r": 50, "t": 60},
            ),
            llm_client=object(),
            original_filename="doc.pdf",
        )
    )

    assert result["figure_index"] == 3
    assert result["suggested_action"] == "reclassify_region"
    assert result["resolved_kind"] == "table"
    unit = captured["unit"]
    assert unit.bbox == {"l": 10, "b": 20, "r": 50, "t": 60}


def test_generate_figures_intelligence_batches_by_page_and_falls_back(monkeypatch, tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_c = tmp_path / "c.png"
    for path in (image_a, image_b, image_c):
        path.write_bytes(b"fake-image")

    requested = {}
    fallback_calls = []

    async def _fake_request_llm_json(*, llm_client, content, schema_name, response_schema, cache_breakpoint_index=None):
        requested["schema_name"] = schema_name
        requested["cache_breakpoint_index"] = cache_breakpoint_index
        requested["content"] = content
        return {
            "task_type": "figure_batch_intelligence",
            "decisions": [
                {
                    "figure_index": 1,
                    "summary": "Meaningful chart.",
                    "confidence": "high",
                    "suggested_action": "set_alt_text",
                    "reason": "Bars and axis labels are clear.",
                    "alt_text": "Bar chart of yearly counts.",
                },
                {
                    "figure_index": 2,
                    "summary": "Redundant ornament.",
                    "confidence": "high",
                    "suggested_action": "mark_decorative",
                    "reason": "Decorative flourish only.",
                    "is_decorative": True,
                },
            ],
        }

    async def _fake_single(**kwargs):
        fallback_calls.append(kwargs["figure"].index)
        return {
            "task_type": "figure_intelligence",
            "summary": "Fallback figure review result.",
            "confidence": "medium",
            "confidence_score": 0.6,
            "suggested_action": "manual_only",
            "reason": "Fallback used.",
            "figure_index": kwargs["figure"].index,
            "alt_text": "",
            "resolved_kind": None,
            "is_decorative": False,
        }

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.request_llm_json",
        _fake_request_llm_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.generate_figure_intelligence",
        _fake_single,
    )

    figures = [
        FigureInfo(index=1, path=image_a, caption="A", page=0),
        FigureInfo(index=2, path=image_b, caption="B", page=0),
        FigureInfo(index=3, path=image_c, caption="C", page=0),
    ]

    results = asyncio.run(
        generate_figures_intelligence(
            figures=figures,
            llm_client=object(),
        )
    )

    assert requested["schema_name"] == "figure_batch_intelligence"
    assert any(item.get("type") == "text" and "Context JSON" in item.get("text", "") for item in requested["content"])
    assert [result["figure_index"] for result in results] == [1, 2, 3]
    assert results[0]["suggested_action"] == "set_alt_text"
    assert results[1]["suggested_action"] == "mark_decorative"
    assert results[2]["suggested_action"] == "manual_only"
    assert fallback_calls == [3]


def test_generate_alt_text_uses_batched_figure_intelligence(monkeypatch, tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_a.write_bytes(b"fake-image")
    image_b.write_bytes(b"fake-image")
    captured = {}

    async def _fake_batch(*, figures, llm_client, job=None, original_filename=""):
        captured["indexes"] = [figure.index for figure in figures]
        captured["original_filename"] = original_filename
        return [
            {
                "task_type": "figure_intelligence",
                "summary": "Figure 1",
                "confidence": "high",
                "confidence_score": 0.9,
                "suggested_action": "set_alt_text",
                "reason": "Meaningful.",
                "figure_index": 1,
                "alt_text": "Chart showing enrollment.",
                "resolved_kind": None,
                "is_decorative": False,
            },
            {
                "task_type": "figure_intelligence",
                "summary": "Figure 2",
                "confidence": "high",
                "confidence_score": 0.9,
                "suggested_action": "reclassify_region",
                "reason": "This is a table.",
                "figure_index": 2,
                "alt_text": "",
                "resolved_kind": "table",
                "is_decorative": False,
            },
        ]

    monkeypatch.setattr(
        "app.pipeline.alt_text.generate_figures_intelligence",
        _fake_batch,
    )

    results = asyncio.run(
        generate_alt_text(
            [
                FigureInfo(index=1, path=image_a, caption="Enrollment", page=0),
                FigureInfo(index=2, path=image_b, caption="Return codes", page=0),
            ],
            object(),
            original_filename="doc.pdf",
        )
    )

    assert captured["indexes"] == [1, 2]
    assert captured["original_filename"] == "doc.pdf"
    assert results[0].figure_index == 1 and results[0].generated_text == "Chart showing enrollment."
    assert results[1].figure_index == 2 and results[1].status == "reclassified" and results[1].resolved_kind == "table"
