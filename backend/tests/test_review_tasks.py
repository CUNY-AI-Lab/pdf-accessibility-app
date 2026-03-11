import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.review import (
    _accept_recommendation_without_changes,
    _garbled_text_followup_spec,
    _post_tagging_font_remediation_preservation,
    _refresh_recommendation_review_status,
    _sync_llm_followup_tasks,
)
from app.models import Base, Job, ReviewTask


def test_post_tagging_font_remediation_preservation_keeps_suggestion_and_attempts():
    task = SimpleNamespace(task_type="font_text_fidelity", source="validation")
    preserved = _post_tagging_font_remediation_preservation(
        task=task,
        task_metadata={
            "llm_suggestion": {"summary": "triangle glyph"},
            "post_tagging_actualtext_attempts": [{"page_number": 2, "operator_index": 132, "actual_text": "pointer"}],
        },
        actualtext_attempts=[{"page_number": 2, "operator_index": 194, "actual_text": "pointer"}],
    )

    metadata = preserved[("font_text_fidelity", "validation")]
    assert metadata["llm_suggestion"] == {"summary": "triangle glyph"}
    assert metadata["post_tagging_actualtext_attempts"] == [
        {"page_number": 2, "operator_index": 132, "actual_text": "pointer"},
        {"page_number": 2, "operator_index": 194, "actual_text": "pointer"},
    ]


def test_post_tagging_font_remediation_preservation_keeps_font_mapping_attempts():
    task = SimpleNamespace(task_type="font_text_fidelity", source="validation")
    preserved = _post_tagging_font_remediation_preservation(
        task=task,
        task_metadata={
            "post_tagging_font_mapping_attempts": [
                {"page_number": 2, "operator_index": 132, "unicode_text": "►", "font_code_hex": "01"},
            ],
        },
        font_mapping_attempts=[
            {"page_number": 2, "operator_index": 194, "unicode_text": "►", "font_code_hex": "01"},
        ],
    )

    metadata = preserved[("font_text_fidelity", "validation")]
    assert metadata["post_tagging_font_mapping_attempts"] == [
        {"page_number": 2, "operator_index": 132, "unicode_text": "►", "font_code_hex": "01"},
        {"page_number": 2, "operator_index": 194, "unicode_text": "►", "font_code_hex": "01"},
    ]


def test_garbled_text_followup_spec_only_uses_blocking_hints():
    parent_task = SimpleNamespace(id=17, task_type="reading_order")

    spec = _garbled_text_followup_spec(
        parent_task=parent_task,
        suggestion={
            "summary": "Broken extraction on the title page.",
            "readable_text_hints": [
                {
                    "page": 1,
                    "review_id": "review-2",
                    "extracted_text": "D a t a  B o o k",
                    "native_text_candidate": "D a t a  B o o k",
                    "ocr_text_candidate": "Data Book",
                    "readable_text_hint": "Data Book",
                    "chosen_source": "ocr",
                    "issue_type": "spacing_only",
                    "confidence": "high",
                    "should_block_accessibility": True,
                    "reason": "The visible title is tightly kerned, but extraction is split.",
                },
                {
                    "page": 1,
                    "review_id": "review-3",
                    "readable_text_hint": "Not blocking",
                    "should_block_accessibility": False,
                },
            ],
        },
    )

    assert spec is not None
    assert spec["task_type"] == "content_fidelity"
    assert spec["blocking"] is True
    assert spec["metadata"]["parent_task_id"] == 17
    assert spec["metadata"]["pages_to_check"] == [1]
    assert spec["metadata"]["flagged_blocks"] == [
        {
            "page": 1,
            "review_id": "review-2",
            "extracted_text": "D a t a  B o o k",
            "native_text_candidate": "D a t a  B o o k",
            "ocr_text_candidate": "Data Book",
            "readable_text_hint": "Data Book",
            "chosen_source": "ocr",
            "issue_type": "spacing_only",
            "confidence": "high",
            "reason": "The visible title is tightly kerned, but extraction is split.",
        }
    ]


@pytest.mark.asyncio
async def test_sync_llm_followup_tasks_creates_and_removes_garbled_text_task():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-1",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="awaiting_recommendation_review",
            input_path="/tmp/sample.pdf",
        )
        parent_task = ReviewTask(
            job_id="job-1",
            task_type="reading_order",
            title="Review reading order",
            detail="Check the reading order on flagged pages.",
            severity="high",
            blocking=True,
            status="pending_review",
            source="validation",
            metadata_json=json.dumps({}),
        )
        db.add(job)
        db.add(parent_task)
        await db.commit()
        await db.refresh(parent_task)

        await _sync_llm_followup_tasks(
            db=db,
            job_id="job-1",
            parent_task=parent_task,
            suggestion={
                "summary": "Garbled title extraction on page 1.",
                "readable_text_hints": [
                    {
                        "page": 1,
                        "review_id": "review-2",
                        "extracted_text": "D a t a  B o o k",
                        "native_text_candidate": "D a t a  B o o k",
                        "ocr_text_candidate": "Data Book",
                        "readable_text_hint": "Data Book",
                        "chosen_source": "ocr",
                        "issue_type": "spacing_only",
                        "confidence": "high",
                        "should_block_accessibility": True,
                        "reason": "Visible text and extracted text diverge.",
                    }
                ],
            },
        )
        await db.commit()

        result = await db.execute(
            select(ReviewTask).where(
                ReviewTask.job_id == "job-1",
                ReviewTask.task_type == "content_fidelity",
            )
        )
        followups = result.scalars().all()
        assert len(followups) == 1
        metadata = json.loads(followups[0].metadata_json or "{}")
        assert metadata["llm_followup_kind"] == "garbled_text_hint"
        assert metadata["parent_task_id"] == parent_task.id
        assert metadata["flagged_blocks"][0]["readable_text_hint"] == "Data Book"
        assert metadata["flagged_blocks"][0]["chosen_source"] == "ocr"

        await _sync_llm_followup_tasks(
            db=db,
            job_id="job-1",
            parent_task=parent_task,
            suggestion={
                "summary": "No remaining accessibility-significant garbling.",
                "readable_text_hints": [
                    {
                        "page": 1,
                        "review_id": "review-2",
                        "readable_text_hint": "Data Book",
                        "should_block_accessibility": False,
                    }
                ],
            },
        )
        await db.commit()

        result = await db.execute(
            select(ReviewTask).where(
                ReviewTask.job_id == "job-1",
                ReviewTask.task_type == "content_fidelity",
            )
        )
        assert result.scalars().all() == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_accept_recommendation_without_changes_resolves_task_and_completes_job():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-accept",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="awaiting_recommendation_review",
            input_path="/tmp/sample.pdf",
            validation_json=json.dumps({"compliant": True, "violations": []}),
        )
        task = ReviewTask(
            job_id="job-accept",
            task_type="table_semantics",
            title="Review table headers",
            detail="Confirm the current table interpretation.",
            severity="high",
            blocking=True,
            status="pending_review",
            source="fidelity",
            metadata_json=json.dumps(
                {
                    "llm_suggestion": {
                        "suggested_action": "confirm_current_headers",
                    }
                }
            ),
        )
        db.add(job)
        db.add(task)
        await db.commit()
        await db.refresh(task)

        response = await _accept_recommendation_without_changes(
            job=job,
            task=task,
            task_metadata=json.loads(task.metadata_json or "{}"),
            suggested_action="confirm_current_headers",
            db=db,
        )

        await db.refresh(job)
        await db.refresh(task)
        metadata = json.loads(task.metadata_json or "{}")

        assert response.status == "accepted"
        assert task.status == "resolved"
        assert metadata["accepted_recommendation"]["suggested_action"] == "confirm_current_headers"
        assert metadata["llm_suggestion"]["suggested_action"] == "confirm_current_headers"
        assert job.status == "complete"

    await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_recommendation_review_status_keeps_job_blocked_when_tasks_remain():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        job = Job(
            id="job-blocked",
            filename="sample.pdf",
            original_filename="sample.pdf",
            status="awaiting_recommendation_review",
            input_path="/tmp/sample.pdf",
            validation_json=json.dumps({"compliant": True, "violations": []}),
        )
        resolved_task = ReviewTask(
            job_id="job-blocked",
            task_type="reading_order",
            title="Review reading order",
            detail="Confirm the current order.",
            severity="high",
            blocking=True,
            status="resolved",
            source="fidelity",
        )
        pending_task = ReviewTask(
            job_id="job-blocked",
            task_type="font_text_fidelity",
            title="Review garbled text",
            detail="Confirm the spoken text fix.",
            severity="high",
            blocking=True,
            status="pending_review",
            source="fidelity",
        )
        db.add(job)
        db.add_all([resolved_task, pending_task])
        await db.commit()

        await _refresh_recommendation_review_status(job=job, db=db)
        await db.refresh(job)

        assert job.status == "awaiting_recommendation_review"

    await engine.dispose()
