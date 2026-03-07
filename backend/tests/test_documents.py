import pytest
from fastapi import HTTPException

from app.api.documents import _sanitize_structure_payload


def test_sanitize_structure_payload_strips_editor_fields_and_clamps_heading_level():
    structure = {
        "title": "Sample",
        "elements": [
            {
                "review_id": "review-1",
                "type": "heading",
                "level": 9,
                "text": "Heading",
                "page": 0,
                "_manual_original_type": "paragraph",
            },
            {
                "review_id": "review-2",
                "type": "artifact",
                "text": "Footer",
                "page": 1,
            },
        ],
    }

    sanitized = _sanitize_structure_payload(structure)

    assert sanitized["title"] == "Sample"
    assert sanitized["elements"][0] == {
        "type": "heading",
        "level": 6,
        "text": "Heading",
        "page": 0,
    }
    assert sanitized["elements"][1] == {
        "type": "artifact",
        "text": "Footer",
        "page": 1,
    }


def test_sanitize_structure_payload_rejects_unsupported_type():
    with pytest.raises(HTTPException) as exc:
        _sanitize_structure_payload(
            {
                "elements": [
                    {"type": "unsupported", "page": 0},
                ]
            }
        )

    assert exc.value.status_code == 400
    assert "Unsupported structure element type" in str(exc.value.detail)
