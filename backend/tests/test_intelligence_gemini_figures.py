import asyncio
from types import SimpleNamespace

from app.pipeline.alt_text import generate_alt_text
from app.pipeline.structure import FigureInfo
from app.services.intelligence_gemini_figures import (
    _figure_page_context,
    _should_suppress_child_ui_alt,
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


def test_figure_page_context_marks_tiny_child_ui_figures(tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_a.write_bytes(b"fake-image")
    image_b.write_bytes(b"fake-image")

    context = _figure_page_context(
        [
            FigureInfo(index=1, path=image_a, page=0, bbox={"l": 0, "b": 0, "r": 500, "t": 300}),
            FigureInfo(index=2, path=image_b, page=0, bbox={"l": 10, "b": 10, "r": 35, "t": 35}),
        ]
    )

    assert context[1]["likely_child_ui_figure"] is False
    assert context[2]["likely_child_ui_figure"] is True
    assert context[2]["larger_sibling_indexes"] == [1]


def test_suppress_child_ui_alt_for_generic_icon_label():
    assert _should_suppress_child_ui_alt(
        raw={
            "suggested_action": "set_alt_text",
            "alt_text": "Magnifying glass icon",
        },
        figure_context={"likely_child_ui_figure": True},
    )

    assert not _should_suppress_child_ui_alt(
        raw={
            "suggested_action": "set_alt_text",
            "alt_text": "Screenshot of the filing table with the magnifying glass icon highlighted.",
        },
        figure_context={"likely_child_ui_figure": True},
    )

    assert not _should_suppress_child_ui_alt(
        raw={
            "suggested_action": "set_alt_text",
            "alt_text": "Magnifying glass",
        },
        figure_context={"likely_child_ui_figure": True},
    )


def test_generate_figure_intelligence_suppresses_generic_child_ui_alt(monkeypatch, tmp_path):
    image_path = tmp_path / "figure.png"
    image_path.write_bytes(b"fake-image")

    async def _fake_adjudicate(*, job, unit, llm_client):
        return SemanticDecision(
            unit_id="figure-18",
            unit_type="figure",
            summary="Tiny icon crop.",
            confidence="high",
            confidence_score=0.9,
            suggested_action="set_alt_text",
            reason="The crop is a small icon.",
            alt_text="Magnifying glass icon",
            is_decorative=False,
        )

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.adjudicate_semantic_unit",
        _fake_adjudicate,
    )

    result = asyncio.run(
        generate_figure_intelligence(
            figure=FigureInfo(index=18, path=image_path, page=8),
            llm_client=object(),
            original_filename="doc.pdf",
            figure_context={"likely_child_ui_figure": True},
        )
    )

    assert result["suggested_action"] == "mark_decorative"
    assert result["is_decorative"] is True
    assert result["alt_text"] == ""


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


def test_generate_figures_intelligence_uses_cached_pdf_page_with_direct_gemini(monkeypatch, tmp_path):
    image_paths = []
    for name in ("a.png", "b.png", "c.png", "d.png", "e.png"):
        path = tmp_path / name
        path.write_bytes(b"fake-image")
        image_paths.append(path)

    fake_pdf_path = tmp_path / "doc.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.7 fake")
    calls = {"cached": [], "created": [], "deleted": [], "fallback": []}

    async def _fake_create_cache(*, pdf_path, page_numbers=None, system_instruction=None, ttl="3600s", settings=None):
        calls["created"].append(
            {
                "pdf_path": pdf_path,
                "page_numbers": list(page_numbers or []),
                "ttl": ttl,
                "system_instruction": system_instruction,
            }
        )
        return SimpleNamespace(cache_name="cache-1", uploaded_file_name="file-1", model_name="gemini")

    async def _fake_cached_json(*, cache_handle, prompt, context_payload=None, response_schema=None, settings=None):
        candidate_indexes = [item["figure_index"] for item in context_payload["candidates"]]
        calls["cached"].append(candidate_indexes)
        return {
            "task_type": "figure_batch_intelligence",
            "decisions": [
                {
                    "figure_index": figure_index,
                    "summary": f"Figure {figure_index}",
                    "confidence": "high",
                    "suggested_action": "set_alt_text",
                    "reason": "Visible chart on the page.",
                    "alt_text": f"Chart {figure_index}",
                }
                for figure_index in candidate_indexes
            ],
        }

    async def _fake_delete_cache(cache_handle, *, settings=None):
        calls["deleted"].append(cache_handle.cache_name)

    async def _unexpected_request_llm_json(**kwargs):
        raise AssertionError("request_llm_json should not be used when cached direct Gemini succeeds")

    async def _unexpected_single(**kwargs):
        calls["fallback"].append(kwargs["figure"].index)
        raise AssertionError("single-figure fallback should not run when cached batch resolves all figures")

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.direct_gemini_pdf_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.job_pdf_path",
        lambda job: fake_pdf_path,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.create_direct_gemini_pdf_cache",
        _fake_create_cache,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.request_direct_gemini_cached_json",
        _fake_cached_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.delete_direct_gemini_pdf_cache",
        _fake_delete_cache,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.request_llm_json",
        _unexpected_request_llm_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.generate_figure_intelligence",
        _unexpected_single,
    )

    figures = [
        FigureInfo(index=index + 1, path=path, caption=f"Figure {index + 1}", page=0)
        for index, path in enumerate(image_paths)
    ]
    job = SimpleNamespace(original_filename="doc.pdf", output_path=str(fake_pdf_path))

    results = asyncio.run(
        generate_figures_intelligence(
            figures=figures,
            llm_client=object(),
            job=job,
            original_filename="doc.pdf",
        )
    )

    assert len(results) == 5
    assert [result["figure_index"] for result in results] == [1, 2, 3, 4, 5]
    assert calls["created"] == [
        {
            "pdf_path": fake_pdf_path,
            "page_numbers": [1],
            "ttl": "900s",
            "system_instruction": "You are evaluating PDF accessibility and figure semantics. Stay grounded in the provided PDF page and return JSON only.",
        }
    ]
    assert calls["cached"] == [[1, 2, 3, 4], [5]]
    assert calls["deleted"] == ["cache-1"]
    assert calls["fallback"] == []


def test_generate_figures_intelligence_direct_gemini_retries_unresolved_batch_items(monkeypatch, tmp_path):
    image_path = tmp_path / "a.png"
    image_path.write_bytes(b"fake-image")
    fake_pdf_path = tmp_path / "doc.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.7 fake")
    fallback_calls = []

    async def _fake_create_cache(*, pdf_path, page_numbers=None, system_instruction=None, ttl="3600s", settings=None):
        return SimpleNamespace(cache_name="cache-1", uploaded_file_name="file-1", model_name="gemini")

    async def _fake_cached_json(*, cache_handle, prompt, context_payload=None, response_schema=None, settings=None):
        return {
            "task_type": "figure_batch_intelligence",
            "decisions": [
                {
                    "figure_index": 1,
                    "summary": "Unclear small figure.",
                    "confidence": "low",
                    "suggested_action": "manual_only",
                    "reason": "Page-level evidence is ambiguous.",
                    "alt_text": "",
                }
            ],
        }

    async def _fake_delete_cache(cache_handle, *, settings=None):
        return None

    async def _fake_single(**kwargs):
        fallback_calls.append(kwargs["figure"].index)
        return {
            "task_type": "figure_intelligence",
            "summary": "Fallback resolved the figure.",
            "confidence": "high",
            "confidence_score": 0.9,
            "suggested_action": "set_alt_text",
            "reason": "Crop clarifies the chart.",
            "figure_index": kwargs["figure"].index,
            "alt_text": "Line chart of enrollment.",
            "resolved_kind": None,
            "is_decorative": False,
        }

    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.direct_gemini_pdf_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.job_pdf_path",
        lambda job: fake_pdf_path,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.create_direct_gemini_pdf_cache",
        _fake_create_cache,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.request_direct_gemini_cached_json",
        _fake_cached_json,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.delete_direct_gemini_pdf_cache",
        _fake_delete_cache,
    )
    monkeypatch.setattr(
        "app.services.intelligence_gemini_figures.generate_figure_intelligence",
        _fake_single,
    )

    job = SimpleNamespace(original_filename="doc.pdf", output_path=str(fake_pdf_path))
    results = asyncio.run(
        generate_figures_intelligence(
            figures=[FigureInfo(index=1, path=image_path, caption="Figure 1", page=0)],
            llm_client=object(),
            job=job,
            original_filename="doc.pdf",
        )
    )

    assert fallback_calls == [1]
    assert results[0]["suggested_action"] == "set_alt_text"
    assert results[0]["alt_text"] == "Line chart of enrollment."
