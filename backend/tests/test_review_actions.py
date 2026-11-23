import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import AppliedChange, Base, Job
from app.services import review_actions as review


@pytest.mark.asyncio
async def test_revise_applied_change_rejects_non_figure_changes():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-2",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash="test-owner",
            status="complete",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-2",
            change_type="table_semantics",
            title="Updated table interpretation",
            detail="The app changed a table header model.",
            importance="high",
            review_status="pending_review",
            reviewable=True,
            metadata_json=json.dumps({}),
            undo_payload_json=json.dumps({}),
        )
        db.add(job)
        db.add(change)
        await db.commit()

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="revise",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 400
        assert exc_info.value.detail == "This change type cannot be revised in the app."
        assert change.review_status == "pending_review"

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_rejects_non_pending_review_change():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-4",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash="test-owner",
            status="complete",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-4",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="kept",
            reviewable=True,
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="undo",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 409
        assert change.review_status == "kept"

    await engine.dispose()


@pytest.mark.asyncio
async def test_undo_applied_change_rejects_failed_jobs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-change-failed-undo",
            filename="sample.pdf",
            original_filename="sample.pdf",
            owner_session_hash="test-owner",
            status="failed",
            input_path="/tmp/sample.pdf",
        )
        change = AppliedChange(
            job_id="job-change-failed-undo",
            change_type="figure_semantics",
            title="Updated figure 1",
            detail="The app revised the figure decision.",
            importance="medium",
            review_status="pending_review",
            reviewable=True,
            undo_payload_json=json.dumps({"kind": "alt_text_entry", "entry_id": 1}),
        )
        db.add_all([job, change])
        await db.commit()

        with pytest.raises(review.ReviewRefusal) as exc_info:
            await review.apply_review_action(
                action="undo",
                job=job,
                change_id=1,
                db=db,
            )

        await db.refresh(change)
        assert exc_info.value.code == 409
        assert (
            exc_info.value.detail
            == "In-app review is only available after the app reaches a complete or manual-remediation output."
        )
        assert change.review_status == "pending_review"

    await engine.dispose()


# ── Asking the model again about a figure (#89) ──────────────────────────


RELAY = "https://tools.test/pdf-accessibility/runner/tasks/job-secret-task/v1"
CANNOT_FIX = {"suggested_action": "manual_only", "alt_text": "", "confidence": "low",
              "summary": "Illegible crop.", "reason": "The crop is too blurred to read."}


def _figure_reply(request):
    import httpx

    return httpx.Response(200, request=request, json={
        "choices": [{"message": {"content": json.dumps(CANNOT_FIX)}}],
    })


async def _revise_figure(tmp_path, monkeypatch, handler):
    """Run the review page's "Ask the model again" on one undescribed figure,
    with the real model client behind a fake relay."""
    import httpx
    from PIL import Image

    from app.config import Settings
    from app.models import AltTextEntry
    from app.services import path_safety
    from app.services.llm_client import LlmClient

    settings = Settings(
        upload_dir=tmp_path / "u", processing_dir=tmp_path / "p", output_dir=tmp_path / "o",
    )
    image = settings.processing_dir / "job-revise" / "figure.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (24, 24), "red").save(image)
    monkeypatch.setattr(path_safety, "get_settings", lambda: settings)
    monkeypatch.setattr(review, "get_settings", lambda: settings)

    def client(_settings):
        llm = LlmClient(base_url=RELAY, api_key="", model="check-model", max_retries=0)
        llm.client = httpx.AsyncClient(base_url=RELAY, transport=httpx.MockTransport(handler))
        return llm

    monkeypatch.setattr(review, "make_llm_client", client)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_maker() as db:
        job = Job(
            id="job-revise", filename="sample.pdf", original_filename="sample.pdf",
            owner_session_hash="test-owner", status="manual_remediation",
            input_path=str(tmp_path / "sample.pdf"),
        )
        entry = AltTextEntry(
            job_id="job-revise", figure_index=0, image_path=str(image), status="pending_review",
        )
        db.add_all([job, entry])
        await db.flush()
        change = AppliedChange(
            job_id="job-revise", change_type="figure_semantics", title="Figure 1 needs a description",
            detail="The app could not describe this figure.", importance="high",
            review_status="pending_review", reviewable=True,
            metadata_json=json.dumps({"figure_index": 0, "page": 1}),
            undo_payload_json=json.dumps({"kind": "alt_text_entry", "entry_id": entry.id}),
        )
        db.add(change)
        await db.commit()
        with pytest.raises(review.ReviewRefusal) as refused:
            await review.apply_review_action(
                action="revise", job=job, change_id=change.id, db=db,
            )
        await db.refresh(change)
        await db.refresh(entry)
    await engine.dispose()
    return refused.value, change, entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (500, {"error": "HTTPStatusError", "status": 500}),
        (429, {"error": "HTTPStatusError", "status": 429}),
        (None, {"error": "ConnectError", "status": None}),
    ],
)
async def test_revise_says_the_model_service_was_unavailable(tmp_path, monkeypatch, status, expected):
    import httpx

    def outage(request):
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, request=request)

    refusal, change, entry = await _revise_figure(tmp_path, monkeypatch, outage)
    answered, _, _ = await _revise_figure(tmp_path / "answered", monkeypatch, _figure_reply)

    assert refusal.code == 503
    assert refusal.service_failure == expected
    assert "job-secret-task" not in refusal.detail
    # Not the "model could not produce a fix" outcome a model answer gets.
    assert refusal.detail != answered.detail
    assert change.review_status == "pending_review"
    assert (entry.status, entry.edited_text) == ("pending_review", None)


@pytest.mark.asyncio
async def test_revise_keeps_its_outcome_when_the_model_answers_without_a_fix(tmp_path, monkeypatch):
    refusal, change, entry = await _revise_figure(tmp_path, monkeypatch, _figure_reply)

    assert refusal.code == 409
    assert refusal.service_failure is None
    assert change.review_status == "pending_review"
    assert entry.status == "pending_review"
