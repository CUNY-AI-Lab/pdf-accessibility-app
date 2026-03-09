import asyncio
from pathlib import Path

from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini_figures import generate_figure_intelligence
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
