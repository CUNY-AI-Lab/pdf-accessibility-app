import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.review import (
    _garbled_text_followup_spec,
    _manual_font_remediation_preservation,
    _manual_review_completion_state,
    _sync_llm_followup_tasks,
    _validated_task_metadata,
)
from app.models import Base, Job, ReviewTask
from app.schemas import ReviewTaskUpdateRequest


def _task(*, blocking: bool, source: str, status: str):
    return SimpleNamespace(blocking=blocking, source=source, status=status)


def _review_task(
    *,
    status: str = "pending_review",
    task_type: str = "generic",
    metadata_json: str | None = None,
):
    return SimpleNamespace(
        status=status,
        task_type=task_type,
        metadata_json=metadata_json,
    )


def test_manual_review_cannot_complete_when_validation_not_compliant():
    ok, reason = _manual_review_completion_state(
        {"compliant": False},
        [_task(blocking=True, source="fidelity", status="resolved")],
    )

    assert ok is False
    assert reason == "Validation still reports unresolved PDF/UA errors"


def test_manual_review_cannot_complete_with_validation_tasks():
    ok, reason = _manual_review_completion_state(
        {"compliant": True},
        [_task(blocking=True, source="validation", status="pending_review")],
    )

    assert ok is False
    assert reason == "Validation-derived remediation tasks cannot be cleared in-app"


def test_manual_review_cannot_complete_with_pending_blocking_fidelity_tasks():
    ok, reason = _manual_review_completion_state(
        {"compliant": True},
        [_task(blocking=True, source="fidelity", status="pending_review")],
    )

    assert ok is False
    assert reason == "1 blocking review task(s) still need review"


def test_manual_review_can_complete_when_only_fidelity_tasks_are_resolved():
    ok, reason = _manual_review_completion_state(
        {"compliant": True},
        [
            _task(blocking=True, source="fidelity", status="resolved"),
            _task(blocking=False, source="fidelity", status="pending_review"),
        ],
    )

    assert ok is True
    assert reason is None


def test_resolving_task_requires_resolution_note():
    task = _review_task()

    with pytest.raises(HTTPException) as exc:
        _validated_task_metadata(
            task,
            ReviewTaskUpdateRequest(status="resolved", resolution_note=""),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Resolution note is required before marking a review task resolved"


def test_resolution_note_is_persisted_in_metadata():
    task = _review_task()

    metadata = _validated_task_metadata(
        task,
        ReviewTaskUpdateRequest(status="resolved", resolution_note="Checked with NVDA"),
    )

    assert metadata["resolution_note"] == "Checked with NVDA"


def test_resolving_task_requires_required_evidence_fields():
    task = _review_task(task_type="reading_order")

    with pytest.raises(HTTPException) as exc:
        _validated_task_metadata(
            task,
            ReviewTaskUpdateRequest(
                status="resolved",
                resolution_note="Checked in NVDA",
                evidence={"verification_method": "NVDA"},
            ),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Missing required review evidence: pages checked"


def test_required_evidence_is_persisted_in_metadata():
    task = _review_task(task_type="reading_order")

    metadata = _validated_task_metadata(
        task,
        ReviewTaskUpdateRequest(
            status="resolved",
            resolution_note="Checked in NVDA",
            evidence={
                "verification_method": "NVDA and text export",
                "pages_checked": "Pages 1-5",
            },
        ),
    )

    assert metadata["resolution_note"] == "Checked in NVDA"
    assert metadata["evidence"] == {
        "verification_method": "NVDA and text export",
        "pages_checked": "Pages 1-5",
    }


def test_manual_font_remediation_preservation_keeps_suggestion_and_attempts():
    task = SimpleNamespace(task_type="font_text_fidelity", source="validation")
    preserved = _manual_font_remediation_preservation(
        task=task,
        task_metadata={
            "llm_suggestion": {"summary": "triangle glyph"},
            "manual_actualtext_attempts": [{"page_number": 2, "operator_index": 132, "actual_text": "pointer"}],
        },
        actualtext_attempts=[{"page_number": 2, "operator_index": 194, "actual_text": "pointer"}],
    )

    metadata = preserved[("font_text_fidelity", "validation")]
    assert metadata["llm_suggestion"] == {"summary": "triangle glyph"}
    assert metadata["manual_actualtext_attempts"] == [
        {"page_number": 2, "operator_index": 132, "actual_text": "pointer"},
        {"page_number": 2, "operator_index": 194, "actual_text": "pointer"},
    ]


def test_manual_font_remediation_preservation_keeps_font_mapping_attempts():
    task = SimpleNamespace(task_type="font_text_fidelity", source="validation")
    preserved = _manual_font_remediation_preservation(
        task=task,
        task_metadata={
            "manual_font_mapping_attempts": [
                {"page_number": 2, "operator_index": 132, "unicode_text": "►", "font_code_hex": "01"},
            ],
        },
        font_mapping_attempts=[
            {"page_number": 2, "operator_index": 194, "unicode_text": "►", "font_code_hex": "01"},
        ],
    )

    metadata = preserved[("font_text_fidelity", "validation")]
    assert metadata["manual_font_mapping_attempts"] == [
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
            status="needs_manual_review",
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
