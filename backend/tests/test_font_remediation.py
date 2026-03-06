from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from app.config import Settings
from app.pipeline import orchestrator
from app.pipeline.orchestrator import (
    FONT_LANE_EMBED,
    FONT_LANE_OCR_REDO,
    FONT_LANE_REPAIR_DICTS,
    FONT_LANE_REPAIR_TOUNICODE,
    _embed_lane_should_skip_local,
    _font_remediation_lanes,
    _merge_tounicode_maps,
    _normalize_font_name,
    _sanitize_text_showing_zero_bytes,
    _simple_font_auto_unicode_policy,
    _simple_font_unicode_map,
    _simple_font_zero_byte_repair_candidate,
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "gemini-test",
    }
    values.update(overrides)
    return Settings(**values)


def _violation(rule_id: str, severity: str = "error") -> SimpleNamespace:
    return SimpleNamespace(rule_id=rule_id, severity=severity)


def test_font_lanes_prioritize_tounicode_when_fonts_are_embedded():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.7-1")],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
    )

    assert lanes[:2] == [FONT_LANE_REPAIR_TOUNICODE, FONT_LANE_EMBED]
    assert skipped == ["OCR lanes skipped: digital document"]


def test_font_lanes_embed_before_tounicode_when_fonts_are_unembedded():
    lanes, skipped = _font_remediation_lanes(
        [
            _violation("ISO 14289-1:2014-7.21.3.2-1"),
            _violation("ISO 14289-1:2014-7.21.7-1"),
        ],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 2},
        settings=_settings(),
    )

    assert lanes[:3] == [
        FONT_LANE_REPAIR_DICTS,
        FONT_LANE_EMBED,
        FONT_LANE_REPAIR_TOUNICODE,
    ]
    assert skipped == ["OCR lanes skipped: digital document"]


def test_ocr_lane_is_not_blocked_by_links_alone():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.8-1")],
        classification="scanned",
        pdf_features={"pages": 5, "link_annots": 3, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
    )

    assert FONT_LANE_OCR_REDO in lanes
    assert skipped == []


def test_unicode_lane_is_skipped_when_gate_has_no_safe_candidates():
    lanes, skipped = _font_remediation_lanes(
        [_violation("ISO 14289-1:2014-7.21.7-1")],
        classification="digital",
        pdf_features={"pages": 10, "link_annots": 0, "has_forms": False, "unembedded_fonts": 0},
        settings=_settings(),
        unicode_gate={
            "safe_candidate_count": 0,
            "blocked_candidate_count": 2,
            "reason": "unicode issues appear tied to simple fonts without explicit encoding",
        },
    )

    assert FONT_LANE_REPAIR_TOUNICODE not in lanes
    assert FONT_LANE_EMBED in lanes
    assert skipped == [
        "ToUnicode repair skipped: unicode issues appear tied to simple fonts without explicit encoding",
        "OCR lanes skipped: digital document",
    ]


def test_generated_tounicode_mappings_override_existing_entries():
    merged, overwritten = _merge_tounicode_maps(
        {1: "A", 2: "B"},
        {2: "C", 3: "D"},
    )

    assert merged == {1: "A", 2: "C", 3: "D"}
    assert overwritten == 1


def test_font_name_normalization_strips_subset_prefix_and_punctuation():
    assert _normalize_font_name("/ABCDEE+Arial-BoldMT") == "arialboldmt"
    assert _normalize_font_name("Trebuchet MS Bold") == "trebuchetmsbold"


def test_simple_type1_fonts_fall_back_to_standard_encoding():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
    })

    mapping = _simple_font_unicode_map(font_dict, None)

    assert mapping[65] == "A"


def test_simple_font_policy_blocks_implicit_nonstandard_fonts():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/ABCDEE+TeX_CM_Maths_Symbols"),
    })

    assert _simple_font_auto_unicode_policy(font_dict) == "blocked"


def test_simple_font_zero_byte_repair_candidate_only_allows_code_zero_residue():
    font_dict = pikepdf.Dictionary({
        "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Times-Roman"),
        "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
    })

    generated_map = _simple_font_unicode_map(font_dict, None)

    assert _simple_font_zero_byte_repair_candidate(
        font_dict,
        used_codes={0, 65},
        existing_map={},
        generated_map=generated_map,
    ) is True
    assert _simple_font_zero_byte_repair_candidate(
        font_dict,
        used_codes={0, 129, 65},
        existing_map={},
        generated_map=generated_map,
    ) is False


def test_zero_byte_text_operand_sanitizer_strips_only_string_bytes():
    operands = [pikepdf.Array([pikepdf.String(b"ab\x00c"), -120, pikepdf.String(b"\x00d")])]

    new_operands, removed = _sanitize_text_showing_zero_bytes("TJ", operands)

    assert removed == 2
    assert bytes(new_operands[0][0]) == b"abc"
    assert new_operands[0][1] == -120
    assert bytes(new_operands[0][2]) == b"d"


def test_embed_lane_skips_local_when_no_supported_candidates():
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 0,
        }
    }) is True
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 1,
        }
    }) is False
    assert _embed_lane_should_skip_local({
        "summary": {
            "unembedded_fonts": 0,
            "local_embed_candidate_count": 0,
        }
    }) is False


@pytest.mark.asyncio
async def test_font_dict_lane_reuses_existing_tags(tmp_path, monkeypatch):
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")
    reused_tagging = SimpleNamespace(marker="reused")

    async def fake_repair(input_path, output_path):
        output_path.write_bytes(b"%PDF-1.7\n")
        return True, "repaired", {"fonts_touched": 1}

    async def fake_validate(pdf_path, verapdf_path, flavour):
        return SimpleNamespace(
            compliant=True,
            violations=[],
            raw_report={},
            validated_path=Path(pdf_path),
        )

    async def fail_tag_pdf(*args, **kwargs):
        raise AssertionError("tag_pdf should not run for non-structural font repairs")

    monkeypatch.setattr(orchestrator, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "_repair_pdf_font_dicts", fake_repair)
    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate)
    monkeypatch.setattr(orchestrator, "tag_pdf", fail_tag_pdf)

    result = await orchestrator._attempt_font_lane(
        job_id="job-1",
        job=SimpleNamespace(original_filename="sample.pdf"),
        settings=_settings(),
        working_pdf=tmp_path / "working.pdf",
        tagged_pdf=tagged_pdf,
        structure_json={},
        reviewed_alts=[],
        lane=FONT_LANE_REPAIR_DICTS,
        current_tagging_result=reused_tagging,
    )

    assert result["success"] is True
    assert result["requires_retag"] is False
    assert result["tagging_result"] is reused_tagging
    assert result["output_path"] == tmp_path / "fontfix_repair_dicts.pdf"


@pytest.mark.asyncio
async def test_embed_lane_skips_local_attempt_when_diagnostics_rule_it_out(tmp_path, monkeypatch):
    tagged_pdf = tmp_path / "tagged.pdf"
    tagged_pdf.write_bytes(b"%PDF-1.7\n")

    async def fail_local_embed(*args, **kwargs):
        raise AssertionError("_embed_system_fonts should not run when diagnostics skip local embedding")

    async def fake_gs_embed(input_path, output_path):
        output_path.write_bytes(b"%PDF-1.7\n")
        return True, "ghostscript rewrite"

    async def fake_tag_pdf(*args, **kwargs):
        output = kwargs["output_path"]
        output.write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(
            output_path=output,
            headings_tagged=0,
            figures_tagged=0,
            decorative_figures_artifacted=0,
            tables_tagged=0,
            lists_tagged=0,
            links_tagged=0,
            bookmarks_added=0,
            tags_added=0,
            struct_elems_created=0,
            title_set=False,
            lang_set=False,
        )

    async def fake_validate(pdf_path, verapdf_path, flavour):
        return SimpleNamespace(
            compliant=False,
            violations=[],
            raw_report={},
            validated_path=Path(pdf_path),
        )

    monkeypatch.setattr(orchestrator, "create_job_dir", lambda job_id: tmp_path)
    monkeypatch.setattr(orchestrator, "_inspect_font_diagnostics", lambda *args, **kwargs: {
        "summary": {
            "unembedded_fonts": 1,
            "local_embed_candidate_count": 0,
        }
    })
    monkeypatch.setattr(orchestrator, "_embed_system_fonts", fail_local_embed)
    monkeypatch.setattr(orchestrator, "_rewrite_pdf_with_ghostscript_embed", fake_gs_embed)
    monkeypatch.setattr(orchestrator, "tag_pdf", fake_tag_pdf)
    monkeypatch.setattr(orchestrator, "validate_pdf", fake_validate)

    result = await orchestrator._attempt_font_lane(
        job_id="job-embed",
        job=SimpleNamespace(original_filename="sample.pdf"),
        settings=_settings(),
        working_pdf=tmp_path / "working.pdf",
        tagged_pdf=tagged_pdf,
        structure_json={},
        reviewed_alts=[],
        lane=FONT_LANE_EMBED,
        current_tagging_result=SimpleNamespace(),
    )

    assert result["success"] is True
    assert result["requires_retag"] is True
    assert result["details"]["local_embed_skipped"] is True
