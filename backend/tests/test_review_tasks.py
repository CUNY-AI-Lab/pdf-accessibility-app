from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.review import (
    _manual_font_remediation_preservation,
    _manual_review_completion_state,
    _validated_task_metadata,
)
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
