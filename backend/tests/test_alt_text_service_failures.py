"""A figure left undescribed because the model service failed says so (#89).

On Nov 20 the model endpoint returned 500s past the client's retries. Figures
stayed undescribed with nothing to tell that apart from a figure the model
looked at and could not describe; the person was told to add a description by
hand when uploading again would have worked.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.pipeline import alt_text
from app.pipeline.orchestrator import _alt_text_step_result, _record_figure_decisions
from app.pipeline.structure import FigureInfo
from app.services.llm_client import LlmClient, model_service_failure

RELAY = "https://tools.test/pdf-accessibility/runner/tasks/job-secret-task/v1"
STRUCTURE = {"elements": [{"type": "figure", "figure_index": 0, "page": 0}]}


def _figure(tmp_path) -> FigureInfo:
    path = tmp_path / "figure.png"
    Image.new("RGB", (24, 24), "red").save(path)
    return FigureInfo(index=0, path=path, page=0)


def _client(handler) -> LlmClient:
    llm = LlmClient(base_url=RELAY, api_key="", model="check-model", max_retries=0)
    llm.client = httpx.AsyncClient(base_url=RELAY, transport=httpx.MockTransport(handler))
    return llm


def _reply(decision: dict) -> httpx.Response:
    # One payload that every figure prompt (batch, single, description) can read.
    payload = {**decision, "figure_index": 0, "decisions": [{**decision, "figure_index": 0}]}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


CANNOT_DESCRIBE = {
    "suggested_action": "manual_only", "alt_text": "", "confidence": "low",
    "summary": "Illegible crop.", "reason": "The crop is too blurred to read.",
}
DESCRIBED = {
    "suggested_action": "set_alt_text", "alt_text": "A red square on a white page.",
    "confidence": "high", "summary": "Plain shape.", "reason": "Visible shape.",
}


async def _describe(tmp_path, handler, *, initial_only=False):
    settings = Settings(
        upload_dir=tmp_path / "u", processing_dir=tmp_path / "p", output_dir=tmp_path / "o",
        alt_text_cache_enabled=False,
    )
    figure = _figure(tmp_path)
    llm = _client(handler)
    try:
        [result] = await alt_text.generate_alt_text(
            [figure], llm, settings=settings, structure=STRUCTURE,
        )
        if not initial_only:
            await alt_text.describe_unresolved_figures(
                [figure], [result], llm, structure=STRUCTURE,
            )
    finally:
        await llm.close()
    return figure, result


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_only", [True, False])
@pytest.mark.parametrize(
    "outage,expected",
    [
        (lambda request: httpx.Response(500, request=request), {"error": "HTTPStatusError", "status": 500}),
        (
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
            {"error": "ConnectError", "status": None},
        ),
    ],
)
async def test_outage_is_recorded_per_figure_and_keeps_the_hold(tmp_path, outage, expected, initial_only):
    requests = []

    def handler(request):
        requests.append(request)
        return outage(request)

    figure, result = await _describe(tmp_path, handler, initial_only=initial_only)

    assert requests
    assert result.status == "pending_review"
    assert result.generated_text == ""
    assert result.service_failure == expected
    # The record keeps the error class and status, never the relay URL.
    assert "job-secret-task" not in json.dumps([result.reason, result.service_failure])
    [spec] = alt_text.figure_applied_change_specs(figures=[figure], alt_texts=[result])
    assert spec["metadata"]["model_service_failure"] == expected
    assert spec["after"]["status"] == "pending_review"


@pytest.mark.asyncio
async def test_a_model_that_answers_but_cannot_describe_is_not_a_service_failure(tmp_path):
    figure, result = await _describe(tmp_path, lambda request: _reply(CANNOT_DESCRIBE))

    assert result.status == "pending_review"
    assert result.service_failure is None
    [spec] = alt_text.figure_applied_change_specs(figures=[figure], alt_texts=[result])
    assert "model_service_failure" not in spec["metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery,status",
    [(DESCRIBED, "approved"), (CANNOT_DESCRIBE, "pending_review")],
)
async def test_an_answer_in_recovery_clears_the_initial_failure(tmp_path, recovery, status):
    requests = []

    def handler(request):
        requests.append(request)
        # The batch and single requests fail; the recovery request is answered.
        if len(requests) <= 2:
            return httpx.Response(503, request=request)
        return _reply(recovery)

    _, result = await _describe(tmp_path, handler)

    assert len(requests) >= 3
    assert result.status == status
    assert result.service_failure is None


def test_step_result_accounts_for_every_figure():
    failure = {"error": "HTTPStatusError", "status": 500}
    alt_texts = [
        alt_text.AltTextResult(figure_index=0, generated_text="A bar chart of enrollment.", status="approved"),
        alt_text.AltTextResult(figure_index=1, generated_text="decorative", status="rejected"),
        alt_text.AltTextResult(figure_index=2, generated_text="", status="pending_review"),
        alt_text.AltTextResult(
            figure_index=3, generated_text="", status="pending_review", service_failure=failure,
        ),
        alt_text.AltTextResult(
            figure_index=4, generated_text="", status="reclassified", resolved_kind="table",
        ),
    ]
    figures = [SimpleNamespace(index=i, path=f"{i}.png", caption=None) for i in range(5)]
    rows = []

    outcomes = _record_figure_decisions(
        SimpleNamespace(add=rows.append), "job-1", alt_texts, figures,
        removed_indexes=[4], auto_approve=True,
    )
    result = _alt_text_step_result(
        count=len(alt_texts), **outcomes, reviewable_changes=0,
        auto_approve_enabled=True,
    )

    assert (result["approved"], result["rejected"], result["reclassified"]) == (1, 1, 1)
    assert (result["needs_description"], result["service_failed"]) == (1, 1)
    assert result["service_failures"] == [{"figure_index": 3, **failure}]
    assert sum(
        result[key] for key in ("approved", "rejected", "reclassified", "needs_description", "service_failed")
    ) == result["count"]
    assert sorted((row.figure_index, row.status) for row in rows) == [
        (0, "approved"), (1, "rejected"), (2, "pending_review"), (3, "pending_review"),
    ]


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", RELAY + "/chat/completions")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status, request=request),
    )


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_status_error(500), {"error": "HTTPStatusError", "status": 500}),
        (_status_error(429), {"error": "HTTPStatusError", "status": 429}),
        (httpx.ReadTimeout("slow"), {"error": "ReadTimeout", "status": None}),
        # A rejected request or an unreadable answer is not an outage.
        (_status_error(400), None),
        (ValueError("Unexpected LLM response format"), None),
    ],
)
def test_only_an_unanswered_request_is_a_service_failure(exc, expected):
    assert model_service_failure(exc) == expected
