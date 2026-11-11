"""The stored font remediation JSON contract survives the dataclass extraction."""

import json

from app.pipeline.fonts.result import FontRemediation


def test_font_remediation_serializes_the_existing_twenty_keys_and_values():
    features = {"pages": 3, "has_forms": False}
    result = FontRemediation(
        document_features=features,
        first_pass_errors=2,
        first_pass_warnings=1,
    )
    expected = {
        "attempted": False,
        "eligible": False,
        "applied": False,
        "strategy": "safe_first_non_destructive",
        "document_features": features,
        "unicode_gate": None,
        "preflight_diagnostics": None,
        "postflight_diagnostics": None,
        "first_pass_errors": 2,
        "first_pass_warnings": 1,
        "second_pass_errors": None,
        "second_pass_warnings": None,
        "lanes_planned": [],
        "lanes_skipped": [],
        "lane_results": [],
        "selected_lane": None,
        "selected_lanes": [],
        "error": None,
        "ocr_message": "",
        "ocr_skipped": False,
    }
    assert result.to_dict() == expected
    assert json.dumps(result.to_dict()) == json.dumps(expected)
    result.selected_preprocessed_path = "/tmp/ocred.pdf"
    expected["selected_preprocessed_path"] = "/tmp/ocred.pdf"
    assert json.dumps(result.to_dict()) == json.dumps(expected)


def test_font_remediation_keeps_lane_results_separate_between_jobs():
    first = FontRemediation()
    second = FontRemediation()
    first.lane_results.append({"lane": "embed_fonts", "success": True})
    first.lanes_planned.append("embed_fonts")
    assert second.lane_results == []
    assert second.lanes_planned == []
