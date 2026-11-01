"""Recorded figure reply shapes and honest recovery of undescribed figures."""

import pytest
from PIL import Image

from app.pipeline import alt_text
from app.pipeline.orchestrator import _alt_text_entry_state, _apply_figure_reclassification
from app.pipeline.structure import FigureInfo
from app.services import intelligence_figures, intelligence_semantics
from app.services.figure_decisions import normalize_figure_decision
from app.services.semantic_units import SemanticUnit


@pytest.mark.parametrize("field", ["action", "decision", "suggested_action"])
@pytest.mark.parametrize("action", ["set_alt_text", "mark_decorative", "reclassify_region"])
def test_observed_action_fields(field, action):
    result = normalize_figure_decision(
        {field: action, "alt_text": "Three bars rise from left to right."}
    )
    assert result["suggested_action"] == action
    assert result["alt_text"] == "Three bars rise from left to right."


@pytest.mark.asyncio
async def test_semantic_figure_parser_honors_recorded_decoration(monkeypatch):
    async def reply(**kwargs):
        return {"unit_id": "figure-6", "unit_type": "figure", "action": "mark_decorative"}

    monkeypatch.setattr(intelligence_semantics, "request_llm_json", reply)
    unit = SemanticUnit(
        unit_id="figure-6", unit_type="figure", page=1, accessibility_goal="Describe"
    )
    result = await intelligence_semantics.adjudicate_semantic_unit(
        job=None, unit=unit, llm_client=None
    )
    assert result.suggested_action == "mark_decorative"


@pytest.mark.asyncio
async def test_single_decision_batch_maps_only_explicit_candidate(monkeypatch, tmp_path):
    figures = []
    for index in range(13):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (8, 8), "white").save(path)
        figures.append(FigureInfo(index=index, path=path, page=0))
    requests = []
    followups = []

    async def batch(**kwargs):
        import json

        payload = next(
            part["text"]
            for part in kwargs["content"]
            if part.get("text", "").startswith("Context JSON:")
        )
        candidates = json.loads(payload.split("\n", 1)[1])["candidates"]
        requests.append([candidate["figure_index"] for candidate in candidates])
        return {
            "figure_index": candidates[0]["figure_index"],
            "suggested_action": "keep_figure_semantics",
            "set_alt_text": "A red circle above a blue square.",
            "confidence": "high",
        }

    async def single(*, figure, **kwargs):
        followups.append(figure.index)
        return {
            "figure_index": figure.index,
            "suggested_action": "set_alt_text",
            "alt_text": "A blue square below a red circle.",
            "confidence": "high",
        }

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", single)
    results = await intelligence_figures.generate_figures_intelligence(
        figures=figures, llm_client=None
    )
    assert [result["figure_index"] for result in results] == list(range(13))
    assert requests == [list(range(i, min(i + 4, 13))) for i in range(0, 13, 4)]
    assert followups == [1, 2, 3, 5, 6, 7, 9, 10, 11]
    assert all(result["alt_text"] for result in results)


def test_uncovered_table_keeps_recorded_description():
    result = alt_text.AltTextResult(
        figure_index=0,
        generated_text="Calendar grid with weekday columns and event dates.",
        status="reclassified",
        resolved_kind="table",
    )
    structure = {"elements": [{"type": "figure", "figure_index": 0, "page": 0}]}
    updated, _ = _apply_figure_reclassification(structure, [result])
    assert updated == structure
    assert _alt_text_entry_state(result, caption=None, auto_approve=True) == (
        "approved",
        result.generated_text,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,expected",
    [
        (
            {
                "suggested_action": "set_alt_text",
                "alt_text": "A calendar of events arranged in seven weekday columns.",
            },
            "approved",
        ),
        ({"suggested_action": "mark_decorative"}, "rejected"),
        ({"suggested_action": "manual_only", "alt_text": ""}, "pending_review"),
        (
            {"suggested_action": "set_alt_text", "alt_text": "[Generation failed: timeout]"},
            "pending_review",
        ),
    ],
)
async def test_targeted_recovery_uses_pixels_or_preserves_hold(
    monkeypatch, tmp_path, reply, expected
):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(
        figure_index=0, generated_text="", status="pending_review", resolved_kind="table"
    )
    calls = []

    async def recover(**kwargs):
        calls.append(kwargs)
        return reply

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", recover)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert result.status == expected
    assert "No tagged table or form" in calls[0]["reviewer_feedback"]
    assert result.generated_text or expected == "pending_review"
    if expected == "pending_review":
        assert _alt_text_entry_state(result, caption=None, auto_approve=True) == (
            "pending_review",
            "",
        )


@pytest.mark.asyncio
async def test_recovery_never_hides_referenced_content(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, caption="Figure 1. Campus map", page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def decorative(**kwargs):
        return {"suggested_action": "mark_decorative"}

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", decorative)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert result.status == "pending_review"


@pytest.mark.asyncio
async def test_failed_single_followup_retains_usable_batch_description(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)

    async def batch(**kwargs):
        return {
            "decisions": [
                {
                    "figure_index": 0,
                    "suggested_action": "set_alt_text",
                    "alt_text": "A red circle beside a blue square.",
                    "confidence": "low",
                }
            ]
        }

    async def failed(**kwargs):
        return {"suggested_action": "manual_only", "alt_text": "", "reason": "timeout"}

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", failed)
    [result] = await intelligence_figures.generate_figures_intelligence(
        figures=[FigureInfo(index=0, path=path, page=0)],
        llm_client=None,
    )
    assert result["alt_text"] == "A red circle beside a blue square."


@pytest.mark.asyncio
@pytest.mark.parametrize("decorative", [False, True])
async def test_recovery_reaches_the_screen_reader(monkeypatch, tmp_path, decorative):
    from app.pipeline.tagger import tag_pdf
    from tests.test_figure_descriptions import FIGURE_STRUCTURE, _figure_alts, _figure_pdf

    pdf = _figure_pdf(tmp_path / "input.pdf")
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def recover(**kwargs):
        return {
            "suggested_action": "mark_decorative" if decorative else "set_alt_text",
            "alt_text": "A calendar grid with seven weekday columns.",
        }

    monkeypatch.setattr(alt_text, "generate_figure_intelligence", recover)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure=FIGURE_STRUCTURE)
    status, text = _alt_text_entry_state(result, caption=None, auto_approve=True)
    output = tmp_path / "tagged.pdf"
    await tag_pdf(
        input_path=pdf,
        output_path=output,
        structure_json=FIGURE_STRUCTURE,
        alt_texts=[
            {"figure_index": 0, "status": status, "text": text, "decorative": status == "rejected"}
        ],
        original_filename="input.pdf",
    )
    assert _figure_alts(output) == (
        [] if decorative else ["A calendar grid with seven weekday columns."]
    )


@pytest.mark.asyncio
async def test_image_aware_decoration_can_correct_coarse_classifier(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "red").save(path)
    figure = FigureInfo(index=0, path=path, page=0, classification="photograph")

    async def decision(**kwargs):
        return [
            {
                "suggested_action": "mark_decorative",
                "reason": "Repeated red silhouette header ornament.",
                "confidence": "high",
            }
        ]

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", decision)
    [result] = await alt_text.generate_alt_text([figure], None, structure={})
    assert result.status == "rejected"
    assert result.generated_text == "decorative"


@pytest.mark.asyncio
async def test_uncovered_reclassification_retains_batch_description(monkeypatch, tmp_path):
    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)

    async def batch(**kwargs):
        return {
            "figure_index": 0,
            "suggested_action": "keep_figure",
            "set_alt_text": "Screenshot of search filters with selected subject checkboxes.",
        }

    async def reclassify(**kwargs):
        return {
            "suggested_action": "reclassify_region",
            "resolved_kind": "form_region",
            "alt_text": "",
        }

    monkeypatch.setattr(intelligence_figures, "request_llm_json", batch)
    monkeypatch.setattr(intelligence_figures, "generate_figure_intelligence", reclassify)
    figure = FigureInfo(index=0, path=path, page=0)
    [decision] = await intelligence_figures.generate_figures_intelligence(
        figures=[figure], llm_client=None
    )
    assert decision["description_source"] == "batch"

    async def decisions(**kwargs):
        return [decision]

    monkeypatch.setattr(alt_text, "generate_figures_intelligence", decisions)
    [result] = await alt_text.generate_alt_text([figure], None)
    _apply_figure_reclassification(
        {"elements": [{"type": "figure", "figure_index": 0, "page": 0}]}, [result]
    )
    assert result.status == "approved"
    assert result.generated_text == "Screenshot of search filters with selected subject checkboxes."


@pytest.mark.parametrize("text", [True, 17, {"text": "A chart"}, ["A chart"]])
def test_nonstring_model_payloads_do_not_become_descriptions(text):
    result = normalize_figure_decision({"action": {"set_alt_text": True}, "alt_text": text})
    assert result["alt_text"] == ""
    assert result["suggested_action"] == "manual_only"


@pytest.mark.asyncio
async def test_request_timeout_during_recovery_keeps_release_hold(monkeypatch, tmp_path):
    import httpx

    path = tmp_path / "f.png"
    Image.new("RGB", (20, 20), "white").save(path)
    figure = FigureInfo(index=0, path=path, page=0)
    result = alt_text.AltTextResult(figure_index=0, generated_text="", status="pending_review")

    async def timeout(**kwargs):
        raise httpx.ReadTimeout("offline timeout")

    monkeypatch.setattr(intelligence_figures, "adjudicate_semantic_unit", timeout)
    await alt_text.describe_unresolved_figures([figure], [result], None, structure={})
    assert _alt_text_entry_state(result, caption=None, auto_approve=True) == ("pending_review", "")


@pytest.mark.parametrize("field", ["set_alt_text", "resolved_alt_text", "resolved_text"])
def test_observed_description_payload_fields(field):
    result = normalize_figure_decision(
        {"action": "set_alt_text", field: "A face with numbered landmarks."}
    )
    assert result["alt_text"] == "A face with numbered landmarks."


@pytest.mark.parametrize(
    "placeholder", ["N/A", "No description available.", "Unable to describe", "[Generation failed]"]
)
def test_boilerplate_descriptions_and_captions_keep_the_hold(placeholder):
    result = alt_text.AltTextResult(figure_index=0, generated_text=placeholder)
    assert _alt_text_entry_state(result, caption=placeholder, auto_approve=True) == (
        "pending_review",
        "",
    )
