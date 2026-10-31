import logging
from pathlib import Path

import pytest

from app.pipeline import fidelity
from app.pipeline.fidelity import (
    _canonical_named_destination,
    _check_internal_link_destinations,
    _check_link_text_quality,
    _collect_structural_fragments,
    _extract_font_review_targets,
    _form_semantics_risk,
    _grounded_text_risk,
    _iter_name_tree_entries,
    _reading_order_metrics,
    _table_semantics_risk,
    assess_fidelity,
)
from tests.fixtures import TEST_SAMPLE_PDF
from tests.pdf_fixtures import described_text, tagged_table, tagged_text_pages


def _validation_report(*, compliant: bool, violations: list[dict], unicode_gate=None) -> dict:
    return {
        "compliant": compliant,
        "violations": violations,
        "summary": {"errors": sum(v.get("count", 1) for v in violations), "warnings": 0},
        "remediation": {
            "font_remediation": {
                "unicode_gate": unicode_gate or {},
            },
        },
        "tagging": {"tables_tagged": 0},
    }


def _blank_output_pdf(tmp_path: Path, name: str = "out.pdf") -> Path:
    """A readable output PDF with no forms or links, for checks that open it."""
    import pikepdf

    path = tmp_path / name
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(path)
    return path


def test_fidelity_creates_blocking_font_task_when_unicode_gate_blocks():
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(
            compliant=False,
            violations=[
                {
                    "rule_id": "ISO 14289-1:2014-7.21.7-1",
                    "severity": "error",
                    "category": "fonts",
                    "description": "Font mapping missing",
                    "count": 5,
                }
            ],
            unicode_gate={"allow_automatic": False, "reason": "ambiguous simple fonts"},
        ),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    assert any(task["task_type"] == "font_text_fidelity" and task["blocking"] for task in tasks)


def test_fidelity_does_not_block_font_task_on_gate_only_without_residual_font_risk():
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report={
            "compliant": True,
            "violations": [],
            "summary": {"errors": 0, "warnings": 0},
            "remediation": {
                "font_remediation": {
                    "unicode_gate": {"allow_automatic": False, "reason": "ambiguous simple fonts"},
                    "postflight_diagnostics": {
                        "summary": {
                            "fonts_with_unresolved_used_codes": 0,
                            "fonts_with_missing_used_codes": 0,
                        }
                    },
                }
            },
            "raw_report": {"report": {"jobs": []}},
            "tagging": {"tables_tagged": 0},
        },
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    font_check = next(check for check in report["checks"] if check["check"] == "font_text_fidelity")
    assert font_check["status"] == "pass"
    assert not any(task["task_type"] == "font_text_fidelity" for task in tasks)


def test_fidelity_routes_widget_alt_rule_to_form_semantics():
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(
            compliant=False,
            violations=[
                {
                    "rule_id": "ISO 14289-1:2014-7.18.1-3",
                    "severity": "error",
                    "category": None,
                    "description": (
                        "A form field shall have a TU key present or all its Widget annotations shall "
                        "have alternative descriptions (in the form of an Alt entry in the enclosing structure elements)"
                    ),
                    "count": 1,
                }
            ],
        ),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    assert any(task["task_type"] == "form_semantics" and task["blocking"] for task in tasks)
    assert not any(task["task_type"] == "alt_text" for task in tasks)


def test_fidelity_form_semantics_fails_when_validator_still_has_form_errors(monkeypatch):
    monkeypatch.setattr(
        fidelity,
        "_form_semantics_risk",
        lambda output_pdf: {
            "field_count": 10,
            "missing_labels": 0,
            "weak_labels": 0,
            "targets": [],
        },
    )

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(
            compliant=False,
            violations=[
                {
                    "rule_id": "ISO 14289-1:2014-7.18.1-3",
                    "severity": "error",
                    "category": None,
                    "description": (
                        "A form field shall have a TU key present or all its Widget annotations shall "
                        "have alternative descriptions (in the form of an Alt entry in the enclosing structure elements)"
                    ),
                    "count": 3,
                }
            ],
        ),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    form_check = next(check for check in report["checks"] if check["check"] == "form_semantics")
    assert form_check["status"] == "warning"
    assert form_check["metrics"]["remaining_validation_errors"] == 3
    form_task = next(task for task in tasks if task["task_type"] == "form_semantics")
    assert form_task["blocking"] is True
    assert form_task["metadata"]["remaining_validation_errors"] == 3


def test_fidelity_routes_figure_alt_rule_to_alt_text_not_table_semantics():
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(
            compliant=False,
            violations=[
                {
                    "rule_id": "ISO 14289-1:2014-7.3-1",
                    "severity": "error",
                    "category": None,
                    "description": (
                        "Figure tags shall include an alternative representation or replacement text that "
                        "represents the contents marked with the Figure tag as noted in ISO 32000-1:2008, 14.7.2, Table 323"
                    ),
                    "count": 3,
                }
            ],
        ),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    assert any(task["task_type"] == "alt_text" and task["blocking"] for task in tasks)
    assert not any(task["task_type"] == "table_semantics" for task in tasks)


def test_extract_font_review_targets_from_verapdf_contexts():
    raw_report = {
        "report": {
            "jobs": [
                {
                    "validationResult": [
                        {
                            "details": {
                                "ruleSummaries": [
                                    {
                                        "specification": "ISO 14289-1:2014",
                                        "clause": "7.21.7",
                                        "testNumber": 1,
                                        "checks": [
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[1](10 0 obj PDPage)/"
                                                    "contentStream[0](26 0 obj PDSemanticContentStream)/"
                                                    "operators[132]/usedGlyphs[0]"
                                                    "(HHCEDJ+MathematicalPi-Six HHCEDJ+MathematicalPi-Six "
                                                    "1 0 1490985125 3 true)"
                                                ),
                                            },
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[4](20 0 obj PDPage)/resources/"
                                                    "xObject[0](Fx0 7 0 obj PDFormXObject)/"
                                                    "contentStream[0](8 0 obj PDSemanticContentStream)/"
                                                    "operators[98]/usedGlyphs[0]"
                                                    "(NCOSCJ+TeX_CM_Maths_Symbols NCOSCJ+TeX_CM_Maths_Symbols "
                                                    "1 0 1490985125 3 true)"
                                                ),
                                            },
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    targets, pages, fonts = _extract_font_review_targets(
        raw_report,
        {"ISO 14289-1:2014-7.21.7-1"},
    )

    assert pages == [2, 5]
    assert fonts == ["MathematicalPi-Six", "TeX_CM_Maths_Symbols"]
    assert targets[0]["page"] == 2
    assert targets[0]["font"] == "MathematicalPi-Six"
    assert targets[0]["content_stream_index"] == 0
    assert targets[0]["operator_index"] == 132
    assert "pages[1]" in targets[0]["context_path"]
    assert targets[1]["page"] == 5
    assert targets[1]["font"] == "TeX_CM_Maths_Symbols"
    assert targets[1]["content_stream_index"] is None
    assert targets[1]["operator_index"] == 98


def test_extract_font_review_targets_from_xobject_context_without_named_object():
    raw_report = {
        "report": {
            "jobs": [
                {
                    "validationResult": [
                        {
                            "details": {
                                "ruleSummaries": [
                                    {
                                        "specification": "ISO 14289-1:2014",
                                        "clause": "7.21.7",
                                        "testNumber": 1,
                                        "checks": [
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[5](559 0 obj PDPage)/"
                                                    "contentStream[0](666 0 obj PDSemanticContentStream)/"
                                                    "operators[21]/xObject[0]/contentStream[0]"
                                                    "(622 0 obj PDSemanticContentStream)/operators[136]/usedGlyphs[0]"
                                                    "(VVGKWT+font0000000025ad2d3c "
                                                    "VVGKWT+font0000000025ad2d3c 92 0 0 true)"
                                                ),
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    targets, pages, fonts = _extract_font_review_targets(
        raw_report,
        {"ISO 14289-1:2014-7.21.7-1"},
    )

    assert pages == [6]
    assert fonts == ["font0000000025ad2d3c"]
    assert targets[0]["page"] == 6
    assert targets[0]["content_stream_index"] == 0
    assert targets[0]["operator_index"] == 136


def test_extract_pdf_text_sample_suppresses_pdfminer_fontbbox_warning(monkeypatch, caplog):
    pdfminer_logger = logging.getLogger("pdfminer.pdffont")

    def _fake_extract_text(*args, **kwargs):
        pdfminer_logger.warning(
            "Could not get FontBBox from font descriptor because None cannot be parsed as 4 floats"
        )
        pdfminer_logger.warning("Different pdfminer warning that should remain visible")
        return "Sample body text"

    monkeypatch.setattr(fidelity, "extract_text", _fake_extract_text)

    with caplog.at_level(logging.WARNING):
        result = fidelity._extract_pdf_text_sample(Path("sample.pdf"))

    assert result == "sample body text"
    messages = [record.getMessage() for record in caplog.records]
    assert not any("Could not get FontBBox from font descriptor because" in message for message in messages)
    assert any("Different pdfminer warning that should remain visible" in message for message in messages)


def test_link_text_quality_ignores_generated_link_fallbacks(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "links.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    good = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/Contents": pikepdf.String("click here"),
        "/A": pikepdf.Dictionary({"/S": pikepdf.Name("/URI"), "/URI": pikepdf.String("https://example.com/a")}),
    }))
    generated = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([50, 10, 80, 20]),
        "/Contents": pikepdf.String("Link to https://example.com/b"),
        "/A": pikepdf.Dictionary({"/S": pikepdf.Name("/URI"), "/URI": pikepdf.String("https://example.com/b")}),
    }))
    page["/Annots"] = pikepdf.Array([good, generated])
    pdf.save(str(pdf_path))

    poor = _check_link_text_quality(pdf_path)

    assert len(poor) == 1
    assert poor[0]["text"] == "click here"


def test_link_text_quality_ignores_generated_internal_destination_labels(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "generated_internal_links.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    generated = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/Contents": pikepdf.String("jump to bibitem cite.AcroRead"),
        "/Dest": pikepdf.Name("/cite.AcroRead"),
    }))
    page["/Annots"] = pikepdf.Array([generated])
    pdf.save(str(pdf_path))

    poor = _check_link_text_quality(pdf_path)

    assert poor == []


def test_link_text_quality_flags_implausibly_long_sentence_like_labels(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "long_link_text.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    paragraph_link = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/Contents": pikepdf.String(
            "Adobe's Acrobat Reader also allows the contents of a PDF to be read out loud and saved "
            "as accessible text, which is not a plausible anchor label for a single link."
        ),
        "/Dest": pikepdf.Name("/cite.AcroRead"),
    }))
    page["/Annots"] = pikepdf.Array([paragraph_link])
    pdf.save(str(pdf_path))

    poor = _check_link_text_quality(pdf_path)

    assert len(poor) == 1
    assert poor[0]["reason"] == "implausibly_long_or_sentence_like"


def test_internal_link_destinations_support_name_objects_and_nested_name_trees(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "internal_links.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    valid = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/Dest": pikepdf.Name("/Section1"),
    }))
    broken = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([50, 10, 80, 20]),
        "/A": pikepdf.Dictionary({
            "/S": pikepdf.Name("/GoTo"),
            "/D": pikepdf.Name("/MissingDest"),
        }),
    }))
    page["/Annots"] = pikepdf.Array([valid, broken])

    kid = pdf.make_indirect(pikepdf.Dictionary({
        "/Names": pikepdf.Array([pikepdf.Name("/Section1"), pikepdf.Array([page.obj, pikepdf.Name("/XYZ"), 0, 0, 0])]),
    }))
    pdf.Root["/Names"] = pikepdf.Dictionary({
        "/Dests": pdf.make_indirect(pikepdf.Dictionary({"/Kids": pikepdf.Array([kid])})),
    })
    pdf.save(str(pdf_path))

    broken_links = _check_internal_link_destinations(pdf_path)

    assert broken_links == [
        {"page": 1, "dest": "MissingDest", "reason": "GoTo destination not found"},
    ]


def test_internal_link_destinations_accept_direct_goto_page_arrays(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "direct_goto.pdf"
    pdf = pikepdf.new()
    page1 = pdf.add_blank_page(page_size=(200, 200))
    page2 = pdf.add_blank_page(page_size=(200, 200))

    direct = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Annot"),
        "/Subtype": pikepdf.Name("/Link"),
        "/Rect": pikepdf.Array([10, 10, 40, 20]),
        "/A": pikepdf.Dictionary({
            "/S": pikepdf.Name("/GoTo"),
            "/D": pikepdf.Array([page2.obj, pikepdf.Name("/XYZ"), 0, 0, 0]),
        }),
    }))
    page1["/Annots"] = pikepdf.Array([direct])
    pdf.save(str(pdf_path))

    broken_links = _check_internal_link_destinations(pdf_path)

    assert broken_links == []


def test_form_semantics_risk_flags_missing_accessible_labels(tmp_path):
    import pikepdf

    pdf_path = tmp_path / "form.pdf"
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    widget = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Widget"),
                "/Rect": pikepdf.Array([10, 10, 80, 28]),
                "/FT": pikepdf.Name("/Tx"),
                "/T": pikepdf.String("f1_01[0]"),
            }
        )
    )
    page["/Annots"] = pikepdf.Array([widget])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({"/Fields": pikepdf.Array([widget])})
    pdf.save(pdf_path)

    risk = _form_semantics_risk(pdf_path)

    assert risk["field_count"] == 1
    assert risk["missing_labels"] == 1
    assert risk["weak_labels"] == 0
    assert risk["targets"][0]["field_type"] == "text"


def test_named_destination_helpers_normalize_and_walk_nested_trees():
    import pikepdf

    kid = pikepdf.Dictionary({
        "/Names": pikepdf.Array([pikepdf.Name("/SectionA"), pikepdf.String("dest")]),
    })
    root = pikepdf.Dictionary({
        "/Kids": pikepdf.Array([kid]),
    })

    assert _canonical_named_destination(pikepdf.Name("/SectionA")) == "SectionA"
    assert _canonical_named_destination(pikepdf.String("SectionB")) == "SectionB"
    entries = _iter_name_tree_entries(root)
    assert len(entries) == 1
    assert entries[0][0] == "SectionA"


def test_collect_structural_fragments_skips_toc_elements():
    fragments = _collect_structural_fragments(
        {
            "elements": [
                {"type": "toc_caption", "text": "Contents"},
                {"type": "toc_item", "text": "1 Introduction ........ 3"},
                {"type": "heading", "text": "Introduction"},
                {"type": "paragraph", "text": "This paragraph should be used for reading-order checks."},
            ]
        }
    )

    assert fragments == [
        "this paragraph should be used for reading order checks",
    ]


def test_scanned_fidelity_advises_when_visible_scan_has_no_ocr_text_or_structure(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "")
    monkeypatch.setattr(
        fidelity,
        "_sample_visual_ink",
        lambda path: {
            "sampled_pages": 1,
            "pages_with_visible_ink": 1,
            "mean_ink_ratio": 0.084,
            "max_ink_ratio": 0.084,
            "visually_blank": False,
        },
    )

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "scan.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="scanned",
    )

    assert report["passed"] is True
    assert any(
        check["check"] == "ocr_coverage" and check["status"] == "warning"
        for check in report["checks"]
    )
    assert any(
        task["task_type"] == "content_fidelity" and not task["blocking"]
        for task in tasks
    )


def test_scanned_fidelity_skips_ocr_coverage_for_visually_blank_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "")
    monkeypatch.setattr(
        fidelity,
        "_sample_visual_ink",
        lambda path: {
            "sampled_pages": 1,
            "pages_with_visible_ink": 0,
            "mean_ink_ratio": 0.0,
            "max_ink_ratio": 0.0,
            "visually_blank": True,
        },
    )

    report, tasks = assess_fidelity(
        input_pdf=Path("scan.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="scanned",
    )

    assert report["passed"] is True
    assert any(
        check["check"] == "ocr_coverage" and check["status"] == "skip"
        for check in report["checks"]
    )
    assert not tasks


def test_fidelity_uses_comparison_source_for_ocr_rescue(monkeypatch, tmp_path):
    broken = "legacy broken text layer " * 20
    clean = "clean recovered text for assistive access " * 20
    samples = {
        "input.pdf": broken,
        "ocr.pdf": clean,
        "out.pdf": clean,
    }
    monkeypatch.setattr(
        fidelity,
        "_extract_pdf_text_sample",
        lambda path: samples.get(Path(path).name, ""),
    )

    report, tasks = assess_fidelity(
        input_pdf=Path("input.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        comparison_source_pdf=Path("ocr.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    text_drift = next(check for check in report["checks"] if check["check"] == "text_drift")
    assert text_drift["status"] == "pass"
    assert text_drift["metrics"]["comparison_source"] == "retag_input"
    assert round(text_drift["metrics"]["original_similarity"], 4) < 0.82
    assert report["passed"] is True
    assert any(
        task["task_type"] == "content_fidelity"
        and task["blocking"] is False
        and task["title"] == "Spot-check OCR rescue text fidelity"
        for task in tasks
    )


def test_extract_font_review_targets_from_annotation_appearance_context():
    raw_report = {
        "report": {
            "jobs": [
                {
                    "validationResult": [
                        {
                            "details": {
                                "ruleSummaries": [
                                    {
                                        "specification": "ISO 14289-1:2014",
                                        "clause": "7.21.7",
                                        "testNumber": 1,
                                        "checks": [
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[0](1 0 obj PDPage)/"
                                                    "annotations[0](12 0 obj PDAnnotation)/"
                                                    "appearanceStream[0](13 0 obj PDFormXObject)/"
                                                    "operators[3]/usedGlyphs[0]"
                                                    "(Annot Annot 1 0 0 0 true)"
                                                ),
                                            },
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    targets, pages, fonts = _extract_font_review_targets(
        raw_report,
        {"ISO 14289-1:2014-7.21.7-1"},
    )

    assert pages == [1]
    assert fonts == ["Annot"]
    assert targets[0]["page"] == 1
    assert targets[0]["font"] == "Annot"
    assert targets[0]["content_stream_index"] is None
    assert targets[0]["operator_index"] == 3


def test_extract_font_review_targets_enriches_local_text_context(tmp_path):
    import pikepdf

    def _resolve_object(value):
        try:
            return value.get_object()
        except Exception:
            return value

    pdf_path = tmp_path / "context.pdf"
    with pikepdf.open(str(TEST_SAMPLE_PDF)) as pdf:
        page = pdf.pages[0]
        page_resources = _resolve_object(page.obj.get("/Resources"))
        fonts = _resolve_object(page_resources.get("/Font"))
        font_name = next(iter(fonts.keys()))
        page["/Contents"] = pdf.make_stream(
            pikepdf.unparse_content_stream(
                [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                    pikepdf.ContentStreamInstruction([font_name, 12], pikepdf.Operator("Tf")),
                    pikepdf.ContentStreamInstruction([10, 20], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("Before text")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([0, -14], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("Target text")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([0, -14], pikepdf.Operator("Td")),
                    pikepdf.ContentStreamInstruction([pikepdf.String("After text")], pikepdf.Operator("Tj")),
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
                ]
            )
        )
        pdf.save(str(pdf_path))

    raw_report = {
        "report": {
            "jobs": [
                {
                    "validationResult": [
                        {
                            "details": {
                                "ruleSummaries": [
                                    {
                                        "specification": "ISO 14289-1:2014",
                                        "clause": "7.21.7",
                                        "testNumber": 1,
                                        "checks": [
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[0](1 0 obj PDPage)/"
                                                    "contentStream[0]/operators[5]/usedGlyphs[0]"
                                                    "(Font Font 1 0 0 0 true)"
                                                ),
                                            },
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    targets, pages, fonts = _extract_font_review_targets(
        raw_report,
        {"ISO 14289-1:2014-7.21.7-1"},
        output_pdf=pdf_path,
    )

    assert pages == [1]
    assert fonts == ["Font"]
    assert targets[0]["decoded_text"] == "Target text"
    assert targets[0]["before_text"] == "Before text"
    assert targets[0]["after_text"] == "After text"


def test_fidelity_allows_advisory_machine_alt_without_blocking(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={
            "elements": [
                {"type": "figure", "figure_index": 0, "caption": "caption"},
                {"type": "heading", "text": "Sample heading for the output"},
            ]
        },
        alt_entries=[
            {
                "figure_index": 0,
                "generated_text": "different generated description",
                "edited_text": None,
                "status": "approved",
            }
        ],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"figures_tagged": 1, "tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is True
    assert any(task["task_type"] == "alt_text" and not task["blocking"] for task in tasks)


def test_fidelity_flags_large_text_drift(monkeypatch, tmp_path):
    samples = {
        "in.pdf": "alpha beta gamma delta " * 80,
        "out.pdf": "omega sigma lambda " * 80,
    }
    monkeypatch.setattr(
        fidelity,
        "_extract_pdf_text_sample",
        lambda path: samples[path.name],
    )

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is True
    assert any(task["task_type"] == "content_fidelity" and not task["blocking"] for task in tasks)


def test_grounded_text_risk_flags_spacing_issue(monkeypatch):
    monkeypatch.setattr("app.services.text_grounding.extract_ocr_text_from_bbox", lambda output_pdf, page_number, bbox: "Data Book")

    risk = _grounded_text_risk(
        Path("out.pdf"),
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "paragraph",
                    "page": 0,
                    "text": "D a t a  B o o k",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
                }
            ]
        },
    )

    assert risk["target_count"] == 1
    assert risk["encoding_problem_count"] == 0
    assert risk["targets"][0]["issue_type"] == "spacing_only"
    assert risk["targets"][0]["ocr_text_candidate"] == "Data Book"


def test_fidelity_warns_on_single_grounded_spacing_issue(monkeypatch):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    monkeypatch.setattr("app.services.text_grounding.extract_ocr_text_from_bbox", lambda output_pdf, page_number, bbox: "Data Book")

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "heading",
                    "page": 0,
                    "text": "D a t a  B o o k",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
                }
            ]
        },
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    grounded = next(check for check in report["checks"] if check["check"] == "grounded_text_fidelity")
    assert grounded["status"] == "warning"
    task = next(
        task for task in tasks
        if task["task_type"] == "content_fidelity"
        and task["metadata"].get("grounded_target_count") == 1
    )
    assert task["blocking"] is False


def test_grounded_text_risk_skips_leaky_ocr_candidate(monkeypatch):
    monkeypatch.setattr(
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        lambda output_pdf, page_number, bbox: (
            "Heading text paragraph continues with neighboring content that should not belong to this block"
        ),
    )

    risk = _grounded_text_risk(
        Path("out.pdf"),
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "heading",
                    "page": 0,
                    "text": "H e a d i n g t e x t",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
                },
                {
                    "review_id": "review-2",
                    "type": "paragraph",
                    "page": 0,
                    "text": "paragraph continues with neighboring content that should not belong to this block",
                    "bbox": {"l": 72, "t": 620, "r": 420, "b": 580},
                },
            ]
        },
    )

    assert risk["target_count"] == 0


def test_grounded_text_risk_skips_ocr_that_contains_native_plus_extra_text(monkeypatch):
    monkeypatch.setattr(
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        lambda output_pdf, page_number, bbox: "Internal Revenue Service data book 2024 additional surrounding text",
    )

    risk = _grounded_text_risk(
        Path("out.pdf"),
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "heading",
                    "page": 0,
                    "text": "I n t e r n a l R e v e n u e S e r v i c e",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
                }
            ]
        },
    )

    assert risk["target_count"] == 0


def test_grounded_text_risk_skips_high_similarity_cleanup(monkeypatch):
    monkeypatch.setattr(
        "app.services.text_grounding.extract_ocr_text_from_bbox",
        lambda output_pdf, page_number, bbox: "Français résumé",
    )

    risk = _grounded_text_risk(
        Path("out.pdf"),
        {
            "elements": [
                {
                    "review_id": "review-1",
                    "type": "paragraph",
                    "page": 0,
                    "text": "FranÃ§ais résumé",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 660},
                }
            ]
        },
    )

    assert risk["target_count"] == 0


def test_fidelity_blocks_multiple_grounded_text_disagreements(monkeypatch):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)

    def _fake_ocr(output_pdf, page_number, bbox):
        if int(bbox["t"]) == 700:
            return "INTERNAL REVENUE SERVICE"
        return "October 1, 2023 to September 30, 2024"

    monkeypatch.setattr("app.services.text_grounding.extract_ocr_text_from_bbox", _fake_ocr)

    structure = {
        "elements": [
            {
                "review_id": "review-1",
                "type": "heading",
                "page": 0,
                "text": "I N T E R N A L R E V E N U E S E R V I C E",
                "bbox": {"l": 72, "t": 700, "r": 420, "b": 660},
            },
            {
                "review_id": "review-2",
                "type": "paragraph",
                "page": 0,
                "text": "O c t o b e r  1,  2023  to  S e p t e m b e r  30,  2024",
                "bbox": {"l": 72, "t": 620, "r": 420, "b": 580},
            },
        ]
    }

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json=structure,
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    grounded = next(check for check in report["checks"] if check["check"] == "grounded_text_fidelity")
    assert grounded["status"] == "warning"
    task = next(
        task for task in tasks
        if task["task_type"] == "content_fidelity"
        and task["metadata"].get("grounded_target_count") == 2
    )
    assert task["blocking"] is False
    assert task["metadata"]["flagged_blocks"][0]["ocr_text_candidate"] == "INTERNAL REVENUE SERVICE"
    assert task["metadata"]["grounded_text_candidate"] is True


def test_reading_order_metrics_tolerate_single_outlier():
    fragments = [
        "alpha section heading long enough",
        "bravo section heading long enough",
        "omega outlier heading long enough",
        "charlie section heading long enough",
        "delta section heading long enough",
        "epsilon section heading long enough",
        "foxtrot section heading long enough",
        "golf section heading long enough",
    ]
    output_text = " ".join([
        fragments[0],
        fragments[1],
        fragments[3],
        fragments[4],
        fragments[5],
        fragments[6],
        fragments[7],
        fragments[2],
    ])

    metrics = _reading_order_metrics(fragments, output_text)

    assert metrics["matched_fragments"] == 8
    assert metrics["ordered_fragments"] == 7
    assert metrics["order_rate"] == 0.875


def test_reading_order_metrics_fall_back_to_dense_matching_for_ocr_spacing():
    fragments = [
        "wa went tip toeing along a path amongst the trees back towards the end of the widow s garden",
        "he listened some more then he come tip toeing down and stood right between us",
    ]
    output_text = (
        "wa went tip toeing along a path amongstthe trees back towards the end of the widow s garden "
        "he listened some more then hecome tip toeing down and stood right between us"
    )

    metrics = _reading_order_metrics(fragments, output_text)

    assert metrics["matched_fragments"] == 2
    assert metrics["ordered_fragments"] == 2
    assert metrics["match_mode"] == "dense"


def test_fidelity_does_not_block_on_single_reading_order_outlier(monkeypatch, tmp_path):
    fragments = [
        "alpha section heading long enough",
        "bravo section heading long enough",
        "omega outlier heading long enough",
        "charlie section heading long enough",
        "delta section heading long enough",
        "epsilon section heading long enough",
        "foxtrot section heading long enough",
        "golf section heading long enough",
    ]
    output_text = (" source text " * 40) + " ".join([
        fragments[0],
        fragments[1],
        fragments[3],
        fragments[4],
        fragments[5],
        fragments[6],
        fragments[7],
        fragments[2],
    ])
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: output_text)

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={"elements": [{"type": "heading", "text": text} for text in fragments]},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is True
    assert not any(task["task_type"] == "reading_order" and task["blocking"] for task in tasks)


def test_fidelity_advises_on_scrambled_pdfminer_reading_order(monkeypatch, tmp_path):
    fragments = [
        "alpha section heading long enough",
        "bravo section heading long enough",
        "charlie section heading long enough",
        "delta section heading long enough",
        "epsilon section heading long enough",
        "foxtrot section heading long enough",
        "golf section heading long enough",
        "hotel section heading long enough",
    ]
    output_text = (" source text " * 40) + " ".join(reversed(fragments))
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: output_text)

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={"elements": [{"type": "heading", "text": text} for text in fragments]},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is True
    assert any(task["task_type"] == "reading_order" and not task["blocking"] for task in tasks)


def test_table_semantics_risk_ignores_simple_regular_table():
    risk = _table_semantics_risk(
        {
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "num_rows": 3,
                    "num_cols": 2,
                    "cells": [
                        {"row": 0, "col": 0, "text": "Program", "column_header": True},
                        {"row": 0, "col": 1, "text": "Students", "column_header": True},
                        {"row": 1, "col": 0, "text": "History", "row_header": True},
                        {"row": 1, "col": 1, "text": "45"},
                        {"row": 2, "col": 0, "text": "Math", "row_header": True},
                        {"row": 2, "col": 1, "text": "50"},
                    ],
                }
            ]
        }
    )

    assert risk["table_count"] == 1
    assert risk["complex_tables"] == 0
    assert risk["high_risk_tables"] == 0
    assert risk["targets"] == []


def test_table_semantics_risk_downgrades_llm_confirmed_header_patterns():
    risk = _table_semantics_risk(
        {
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "review_id": "review-1",
                    "num_rows": 10,
                    "num_cols": 8,
                    "table_llm_confirmed": True,
                    "table_llm_confidence": "high",
                    "table_llm_action": "set_table_headers",
                    "cells": [
                        {"row": 0, "col": 0, "text": "Hdr", "column_header": True, "row_header": True},
                        {"row": 0, "col": 1, "text": "Col1", "column_header": True},
                        {"row": 1, "col": 0, "text": "Row1", "row_header": True},
                        {"row": 1, "col": 1, "text": "Val"},
                        {"row": 0, "col": 2, "text": "Col2", "column_header": True, "col_span": 2},
                    ],
                }
            ]
        }
    )

    assert risk["complex_tables"] == 0
    assert risk["high_risk_tables"] == 0
    assert risk["targets"] == []
    assert risk["risk_score"] == 0.0
    assert risk["table_count"] == 1


def test_table_semantics_risk_keeps_sparse_text_risk_even_when_llm_confirmed():
    risk = _table_semantics_risk(
        {
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "review_id": "review-2",
                    "num_rows": 10,
                    "num_cols": 8,
                    "table_llm_confirmed": True,
                    "table_llm_confidence": "high",
                    "table_llm_action": "confirm_current_headers",
                    "cells": [
                        {"row": 0, "col": 0, "text": "Hdr", "column_header": True, "row_header": True},
                        {"row": 0, "col": 1, "text": "", "column_header": True},
                        {"row": 1, "col": 0, "text": "", "row_header": True},
                        {"row": 1, "col": 1, "text": ""},
                    ],
                }
            ]
        }
    )

    assert risk["complex_tables"] == 1
    assert risk["high_risk_tables"] == 0
    assert risk["targets"][0]["risk_reasons"] == ["sparse cell text"]
    assert risk["targets"][0]["llm_confirmed"] is True


def test_fidelity_flags_high_risk_complex_tables(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "program students history 45")
    monkeypatch.setattr(fidelity, "_sample_visual_ink", lambda path: {
        "sampled_pages": 1,
        "pages_with_visible_ink": 1,
        "mean_ink_ratio": 0.1,
        "max_ink_ratio": 0.1,
        "visually_blank": False,
    })

    structure = {
        "elements": [
            {
                "type": "table",
                "page": 0,
                "bbox": {"l": 72, "t": 700, "r": 520, "b": 200},
                "num_rows": 12,
                "num_cols": 8,
                "cells": [
                    {"row": row, "col": col, "text": ("" if col > 0 else f"r{row}"), "row_span": (2 if row == 0 and col == 0 else 1), "col_span": 1}
                    for row in range(12)
                    for col in range(8)
                ],
            }
        ]
    }

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json=structure,
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 1},
        classification="digital",
    )

    assert report["passed"] is True
    task = next(task for task in tasks if task["task_type"] == "table_semantics")
    assert task["blocking"] is False
    assert task["source"] == "fidelity"
    assert task["metadata"]["complex_tables"] == 1
    assert task["metadata"]["high_risk_tables"] == 1
    assert task["metadata"]["pages_to_check"] == [1]
    assert task["metadata"]["table_review_targets"][0]["table_review_id"] == "review-0"


def test_fidelity_uses_output_struct_tree_for_table_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    monkeypatch.setattr(fidelity, "_output_struct_type_counts", lambda path: {"Document": 1})
    monkeypatch.setattr(fidelity, "_sample_visual_ink", lambda path: {
        "sampled_pages": 1,
        "pages_with_visible_ink": 1,
        "mean_ink_ratio": 0.01,
        "max_ink_ratio": 0.01,
        "visually_blank": False,
    })

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={
            "elements": [
                {
                    "type": "table",
                    "page": 0,
                    "bbox": {"l": 72, "t": 700, "r": 520, "b": 200},
                    "num_rows": 2,
                    "num_cols": 2,
                    "cells": [
                        {"row": 0, "col": 0, "text": "Header", "is_header": True},
                        {"row": 1, "col": 0, "text": "Value"},
                    ],
                }
            ],
        },
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 1},
        classification="digital",
    )

    table_check = next(check for check in report["checks"] if check["check"] == "table_coverage")
    assert report["passed"] is True
    assert table_check["status"] == "warning"
    assert table_check["metrics"]["tagged_tables"] == 0
    task = next(task for task in tasks if task["task_type"] == "table_semantics")
    assert task["blocking"] is False
    assert task["metadata"]["tagged_tables"] == 0


def test_fidelity_advises_when_detected_figures_are_not_tagged(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    monkeypatch.setattr(fidelity, "_sample_visual_ink", lambda path: {
        "sampled_pages": 1,
        "pages_with_visible_ink": 1,
        "mean_ink_ratio": 0.01,
        "max_ink_ratio": 0.01,
        "visually_blank": False,
    })

    report, tasks = assess_fidelity(
        input_pdf=_blank_output_pdf(tmp_path, "in.pdf"),
        output_pdf=_blank_output_pdf(tmp_path),
        structure_json={
            "elements": [
                {
                    "type": "figure",
                    "figure_index": 0,
                    "page": 0,
                    "review_id": "fig-0",
                    "bbox": {"l": 72, "t": 700, "r": 240, "b": 500},
                    "caption": "Figure 1. Diagram",
                },
                {
                    "type": "figure",
                    "figure_index": 1,
                    "page": 2,
                    "bbox": {"l": 72, "t": 400, "r": 240, "b": 200},
                },
            ],
        },
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={
            "figures_tagged": 0,
            "decorative_figures_artifacted": 0,
            "tables_tagged": 0,
        },
        classification="digital",
    )

    assert report["passed"] is True
    figure_check = next(check for check in report["checks"] if check["check"] == "figure_coverage")
    assert figure_check["status"] == "warning"
    assert figure_check["metrics"]["coverage"] == 0.0
    task = next(task for task in tasks if task["task_type"] == "figure_semantics")
    assert task["blocking"] is False
    assert task["metadata"]["detected_figures"] == 2
    assert task["metadata"]["tagged_figures"] == 0
    assert task["metadata"]["pages_to_check"] == [1, 3]
    assert task["metadata"]["figure_review_targets"][0]["figure_review_id"] == "fig-0"


def test_fidelity_counts_decorative_figures_as_covered():
    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={
            "elements": [
                {"type": "figure", "figure_index": 0, "page": 0},
                {"type": "figure", "figure_index": 1, "page": 0},
            ],
        },
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={
            "figures_tagged": 1,
            "decorative_figures_artifacted": 1,
            "tables_tagged": 0,
        },
        classification="digital",
    )

    figure_check = next(check for check in report["checks"] if check["check"] == "figure_coverage")
    assert figure_check["status"] == "pass"
    assert not any(task["task_type"] == "figure_semantics" for task in tasks)


def test_fidelity_font_task_includes_review_targets():
    raw_report = {
        "report": {
            "jobs": [
                {
                    "validationResult": [
                        {
                            "details": {
                                "ruleSummaries": [
                                    {
                                        "specification": "ISO 14289-1:2014",
                                        "clause": "7.21.7",
                                        "testNumber": 1,
                                        "checks": [
                                            {
                                                "status": "failed",
                                                "context": (
                                                    "root/document[0]/pages[1](10 0 obj PDPage)/"
                                                    "contentStream[0]/operators[132]/usedGlyphs[0]"
                                                    "(HHCEDJ+MathematicalPi-Six HHCEDJ+MathematicalPi-Six "
                                                    "1 0 1490985125 3 true)"
                                                ),
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(
            compliant=False,
            violations=[
                {
                    "rule_id": "ISO 14289-1:2014-7.21.7-1",
                    "severity": "error",
                    "category": "fonts",
                    "description": "Font mapping missing",
                    "count": 3,
                }
            ],
            unicode_gate={"allow_automatic": True, "reason": "deterministic font candidates available"},
        ),
        raw_validation_report=raw_report,
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    font_task = next(task for task in tasks if task["task_type"] == "font_text_fidelity")
    assert font_task["metadata"]["font_rule_ids"] == ["ISO 14289-1:2014-7.21.7-1"]
    assert font_task["metadata"]["pages_to_check"] == [2]
    assert font_task["metadata"]["fonts_to_check"] == ["MathematicalPi-Six"]
    assert font_task["metadata"]["font_review_targets"][0]["font"] == "MathematicalPi-Six"
    assert font_task["metadata"]["font_review_targets"][0]["operator_index"] == 132
    assert "context_path" in font_task["metadata"]["font_review_targets"][0]


_BODY_LINES = [
    "Accessible documents keep their text layer intact after remediation.",
    "Screen readers announce the same words that sighted readers see on the page.",
    "This paragraph exists so the text sample is long enough to compare.",
    "Fidelity checks compare the source text with the remediated text.",
    "A remediated file that lost its text must not be released as complete.",
    "The final line closes the sample with a few more ordinary words.",
]


def _text_pdf(path: Path, lines: list[str] = _BODY_LINES) -> Path:
    import pikepdf

    from tests.pdf_fixtures import helvetica

    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=helvetica(pdf)))
    commands = ["/P <</MCID 0>> BDC", "BT", "/F1 11 Tf", "14 TL", "54 720 Td"]
    for line in lines:
        commands.append(f"({line}) Tj T*")
    commands.append("ET EMC")
    page.Contents = pdf.make_stream("\n".join(commands).encode("latin-1"))
    paragraph = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem, S=pikepdf.Name.P, Pg=page.obj, K=0,
    ))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructTreeRoot, K=paragraph,
    ))
    pdf.save(path)
    return path


def _assess_real(input_pdf: Path, output_pdf: Path, classification: str = "digital"):
    return assess_fidelity(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification=classification,
    )


def _check(report: dict, name: str) -> dict:
    return next(check for check in report["checks"] if check["check"] == name)


def test_output_that_keeps_its_text_passes(tmp_path):
    source = _text_pdf(tmp_path / "in.pdf")
    output = _text_pdf(tmp_path / "out.pdf")

    report, tasks = _assess_real(source, output)

    assert report["passed"] is True
    assert _check(report, "text_drift")["status"] == "pass"
    assert not [task for task in tasks if task["blocking"]]


@pytest.mark.parametrize("key", ["Alt", "ActualText"])
@pytest.mark.parametrize("description", ["", "[Figure]", "A chart of enrollment by year"])
def test_only_figures_without_usable_descriptions_hold_the_output(tmp_path, key, description):
    pdf = described_text(tmp_path / "figure.pdf", key=key, description=description)
    report, tasks = _assess_real(pdf, pdf)
    held = description in {"", "[Figure]"}
    assert report["passed"] is not held
    assert _check(report, "figure_descriptions")["status"] == ("fail" if held else "pass")
    assert any(task["blocking"] and task["task_type"] == "alt_text" for task in tasks) is held


def test_residual_font_targets_are_advisory_without_verapdf_failure(tmp_path, monkeypatch):
    pdf = _text_pdf(tmp_path / "out.pdf")
    monkeypatch.setattr(fidelity, "_extract_font_review_targets", lambda *args, **kwargs: (
        [{"page": 1, "font": "F1"}], [1], ["F1"],
    ))
    report, tasks = _assess_real(pdf, pdf)
    assert report["passed"] is True
    assert _check(report, "font_text_fidelity")["status"] == "warning"
    task = next(task for task in tasks if task["task_type"] == "font_text_fidelity")
    assert task["blocking"] is False
    assert task["metadata"]["pages_to_check"] == [1]


def test_form_label_quality_is_advisory_without_verapdf_failure(tmp_path, monkeypatch):
    pdf = _text_pdf(tmp_path / "out.pdf")
    monkeypatch.setattr(fidelity, "_form_semantics_risk", lambda _: {
        "field_count": 1, "missing_labels": 1, "weak_labels": 0,
        "targets": [{"page": 1}],
    })
    report, tasks = _assess_real(pdf, pdf)
    assert report["passed"] is True
    assert _check(report, "form_semantics")["status"] == "warning"
    task = next(task for task in tasks if task["task_type"] == "form_semantics")
    assert task["blocking"] is False


@pytest.mark.parametrize("classification", ["digital", "mixed", "ocr_scan"])
def test_source_text_loss_is_advisory_when_output_has_no_coverage_failure(tmp_path, classification):
    source = _text_pdf(tmp_path / "in.pdf")
    output = _blank_output_pdf(tmp_path)

    report, tasks = _assess_real(source, output, classification)

    assert report["passed"] is True
    assert _check(report, "text_drift")["status"] == "warning"
    lost = [
        task
        for task in tasks
        if task["task_type"] == "content_fidelity" and not task["blocking"]
    ]
    assert len(lost) == 1
    assert lost[0]["metadata"]["output_chars"] == 0
    assert lost[0]["metadata"]["source_chars"] >= fidelity.TEXT_SAMPLE_MIN_CHARS


def test_unreadable_output_is_held_and_every_check_reports_the_error(tmp_path):
    source = _text_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"
    output.write_bytes(b"%PDF-1.7\nthis file was truncated")

    report, tasks = _assess_real(source, output)

    assert report["passed"] is False
    for name in (
        "text_drift", "form_semantics", "link_text_quality", "internal_link_destinations",
        "visual_meaning_gap", "tagged_text_coverage", "table_headers",
    ):
        assert _check(report, name)["status"] == "error", name
    assert _check(report, "reading_order")["status"] == "error"
    blocking_checks = {
        task["metadata"].get("check")
        for task in tasks
        if task["blocking"] and task["task_type"] == "content_fidelity"
    }
    assert {
        "text_drift",
        "form_semantics",
        "link_text_quality",
        "internal_link_destinations",
        "visual_meaning_gap",
        "tagged_text_coverage",
        "table_headers",
    } <= blocking_checks
    # The error is reported without the server's file paths.
    for task in tasks:
        assert str(tmp_path) not in str(task["metadata"].get("error", ""))


def test_unreadable_source_text_is_not_read_as_a_short_source(tmp_path, monkeypatch):
    source = _text_pdf(tmp_path / "in.pdf")
    output = _text_pdf(tmp_path / "out.pdf")
    real_extract = fidelity.extract_text

    def failing_for_source(path, **kwargs):
        if Path(path).name == "in.pdf":
            raise ValueError("pdfminer could not decode a stream")
        return real_extract(path, **kwargs)

    monkeypatch.setattr(fidelity, "extract_text", failing_for_source)

    report, _ = _assess_real(source, output)

    assert report["passed"] is False
    assert _check(report, "text_drift")["status"] == "error"


def test_form_field_check_error_is_not_a_pass(tmp_path, monkeypatch):
    source = _text_pdf(tmp_path / "in.pdf")
    output = _text_pdf(tmp_path / "out.pdf")

    def broken_fields(_path):
        raise KeyError("/Kids")

    monkeypatch.setattr(fidelity, "extract_widget_fields", broken_fields)

    report, tasks = _assess_real(source, output)

    assert report["passed"] is False
    assert _check(report, "form_semantics")["status"] == "error"
    assert any(
        task["blocking"] and task["metadata"].get("check") == "form_semantics" for task in tasks
    )


@pytest.mark.parametrize("invisible", [False, True], ids=["digital", "ocr-layer"])
@pytest.mark.parametrize(("words", "status"), [(20, "warning"), (50, "fail"), (100, "fail")])
def test_artifact_body_is_advisory_unless_catastrophic(tmp_path, invisible, words, status):
    path = tagged_text_pages(tmp_path / "artifact.pdf", hidden_page=1, invisible=invisible, tagged_words=words)
    report, tasks = _assess_real(path, path, "scanned" if invisible else "digital")
    check = _check(report, "tagged_text_coverage")
    assert report["passed"] is (status == "warning")
    assert check["status"] == status
    assert check["metrics"]["coverage"] == 0
    assert check["metrics"]["pages"][0]["coverage"] == 0
    coverage_task = next(task for task in tasks if task["metadata"].get("check") == "tagged_text_coverage")
    assert coverage_task["blocking"] is (status == "fail")
    assert coverage_task["metadata"]["pages_to_check"] == [1]


def test_repeated_running_head_and_page_numbers_are_allowed_artifacts(tmp_path):
    path = tagged_text_pages(tmp_path / "heads.pdf", pages=4, running_head=True, tagged_words=3)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert report["passed"] is True
    assert not [task for task in tasks if task["blocking"]]
    assert check["status"] == "pass"
    assert check["metrics"]["coverage"] == 1
    assert check["metrics"]["excluded_furniture_words"] == 32
    assert all(page["coverage"] == 1 for page in check["metrics"]["pages"])


def test_page_twelve_without_tags_is_held_even_when_overall_coverage_exceeds_threshold(tmp_path):
    path = tagged_text_pages(tmp_path / "long.pdf", pages=12, untagged_page=12, tagged_words=100)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert report["passed"] is False
    assert _check(report, "text_drift")["status"] == "pass"
    assert check["status"] == "fail"
    assert check["metrics"]["coverage"] == pytest.approx(11 / 12)
    assert len(check["metrics"]["pages"]) == 12
    assert check["metrics"]["pages"][-1]["coverage"] == 0
    assert any(task["blocking"] and task["metadata"].get("pages_to_check") == [12] for task in tasks)


@pytest.mark.parametrize(
    ("tagged", "hidden", "status", "blocking"),
    [(85, 15, "warning", False), (90, 10, "warning", False), (190, 10, "warning", False),
     (390, 10, "pass", False), (0, 49, "warning", False), (0, 50, "fail", True),
     (49, 50, "fail", True), (50, 50, "warning", False),
     (97, 3, "pass", False), (96, 4, "warning", False)],
)
def test_tagged_coverage_uses_word_multiplicity_and_threshold_bands(
    tmp_path, tagged, hidden, status, blocking,
):
    path = tagged_text_pages(tmp_path / "band.pdf", tagged_words=tagged, hidden_words=hidden)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == status
    assert check["metrics"]["coverage"] == pytest.approx(tagged / (tagged + hidden))
    assert report["passed"] is not blocking
    coverage_tasks = [task for task in tasks if task["metadata"].get("check") == "tagged_text_coverage"]
    assert len(coverage_tasks) == (0 if status == "pass" else 1)
    if coverage_tasks:
        assert coverage_tasks[0]["blocking"] is blocking


def test_tagged_coverage_honors_actualtext_case_and_punctuation(tmp_path):
    path = tagged_text_pages(
        tmp_path / "actual.pdf", tagged_words=3, actual_text="WORD, Word! word.",
    )
    report, _ = _assess_real(path, path)
    assert report["passed"] is True
    assert _check(report, "tagged_text_coverage")["metrics"]["coverage"] == 1


@pytest.mark.parametrize("headers", [False, True], ids=["no-TH", "with-TH"])
def test_three_by_three_table_requires_header_cells(tmp_path, headers):
    path = tagged_table(tmp_path / "table.pdf", headers=headers, summary=True)
    report, tasks = _assess_real(path, path)
    assert report["passed"] is headers
    check = _check(report, "table_headers")
    assert check["status"] == ("pass" if headers else "fail")
    if not headers:
        task = next(task for task in tasks if task["task_type"] == "table_semantics")
        assert task["blocking"] is True
        assert task["metadata"]["page"] == 1
        assert task["metadata"]["table_index"] == 1
        assert "table 1 on page 1" in task["title"]
        assert check["metrics"]["tables_without_headers"] == [
            {"page": 1, "table_index": 1, "rows": 3, "columns": 3},
        ]


def test_visual_gap_on_unreadable_pdf_records_error_and_holds_document(tmp_path):
    path = tmp_path / "truncated.pdf"
    path.write_bytes(b"%PDF-1.7\ntruncated")
    report, tasks = _assess_real(_text_pdf(tmp_path / "in.pdf"), path)
    assert report["passed"] is False
    assert _check(report, "visual_meaning_gap")["status"] == "error"
    assert any(
        task["blocking"] and task["metadata"].get("check") == "visual_meaning_gap"
        for task in tasks
    )


def test_mapped_table_with_page_references_on_content_still_requires_headers(tmp_path):
    import pikepdf

    path = tagged_table(tmp_path / "mapped.pdf", headers=False)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        root = pdf.Root.StructTreeRoot
        root.RoleMap = pikepdf.Dictionary(CustomTable=pikepdf.Name.Table)
        table = root.K
        table.S = pikepdf.Name("/CustomTable")
        del table.Pg
        for row in table.K.K:
            for cell in row.K:
                cell.K = pikepdf.Dictionary(
                    Type=pikepdf.Name.MCR, Pg=pdf.pages[0].obj, MCID=int(cell.K),
                )
        pdf.save(path)
    report, tasks = _assess_real(path, path)
    assert _check(report, "tagged_text_coverage")["status"] == "pass"
    assert _check(report, "table_headers")["status"] == "fail"
    assert any(task["blocking"] and task["metadata"].get("page") == 1 for task in tasks)


@pytest.mark.parametrize("role", ["Figure", "Formula"])
@pytest.mark.parametrize("key", ["Alt", "ActualText"])
@pytest.mark.parametrize("form", [False, True], ids=["page-MCID", "form-MCID"])
def test_described_figure_or_formula_replaces_only_its_own_words(tmp_path, role, key, form):
    path = described_text(tmp_path / "described.pdf", role=role, key=key, form=form)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert not [task for task in tasks if task["metadata"].get("check") == "tagged_text_coverage"]
    assert check["metrics"]["coverage"] == 1
    assert check["metrics"]["excluded_described_words"] == 20
    assert check["metrics"]["eligible_words"] == 50


@pytest.mark.parametrize("role", ["Figure", "Formula"])
@pytest.mark.parametrize("form", [False, True], ids=["page-MCID", "form-MCID"])
@pytest.mark.parametrize("description", [None, "", "  ", "[Could not determine figure semantics]"])
def test_figure_or_formula_without_alt_exempts_only_its_interior(tmp_path, description, role, form):
    import pikepdf

    path = described_text(tmp_path / "undescribed.pdf", description=description or "", role=role, form=form)
    if description is None:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            del pdf.Root.StructTreeRoot.K[-1].Alt
            pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert check["metrics"]["excluded_figure_words"] == 20
    assert check["metrics"]["eligible_words"] == 50
    if role == "Figure":
        assert _check(report, "figure_descriptions")["status"] == "fail"


def test_described_figure_does_not_exempt_artifact_body(tmp_path):
    path = described_text(tmp_path / "artifact-body.pdf", form=True, hidden_body=True)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "fail"
    assert check["metrics"]["eligible_words"] == 50
    assert check["metrics"]["missing_words"] == 50


@pytest.mark.parametrize(("tagged", "hidden"), [(0, 7), (31, 9), (6, 1)])
def test_tiny_overall_losses_are_advisory(tmp_path, tagged, hidden):
    path = tagged_text_pages(tmp_path / "tiny.pdf", tagged_words=tagged, hidden_words=hidden)
    report, tasks = _assess_real(path, path)
    assert _check(report, "tagged_text_coverage")["status"] == "warning"
    task = next(task for task in tasks if task["metadata"].get("check") == "tagged_text_coverage")
    assert task["blocking"] is False
    assert task["metadata"]["pages_to_check"] == [1]


@pytest.mark.parametrize(("words", "status"), [(99, "warning"), (100, "fail")])
def test_page_blocking_requires_one_hundred_eligible_words(tmp_path, words, status):
    path = tagged_text_pages(tmp_path / "short-page.pdf", pages=12, untagged_page=12, tagged_words=words)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == status
    assert check["metrics"]["pages"][-1]["missing_words"] == words


@pytest.mark.parametrize("missing", [1, 9, 10])
def test_page_only_advisories_name_affected_pages(tmp_path, missing):
    import pikepdf

    path = tagged_text_pages(tmp_path / "page-loss.pdf", pages=12, tagged_words=30)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[-1]
        command = "/Artifact BMC BT /F1 10 Tf 54 400 Td (" + " ".join(["missing"] * missing) + ") Tj ET EMC"
        page.Contents = pdf.make_stream(page.Contents.read_bytes() + b"\n" + command.encode("ascii"))
        pdf.save(path)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "warning"
    assert check["metrics"]["coverage"] > .97
    assert check["metrics"]["pages"][-1]["missing_words"] == missing
    coverage_tasks = [task for task in tasks if task["metadata"].get("check") == "tagged_text_coverage"]
    assert len(coverage_tasks) == 1
    assert coverage_tasks[0]["blocking"] is False
    assert coverage_tasks[0]["metadata"]["pages_to_check"] == [12]
    assert "pages 12" in coverage_tasks[0]["detail"]


def test_figure_replacement_follows_form_invocation_mcid(tmp_path):
    import pikepdf

    path = described_text(tmp_path / "invocation.pdf", form=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(page.Contents.read_bytes().replace(
            b"/Fx Do", b"/Figure <</MCID 1>> BDC /Fx Do EMC",
        ))
        pdf.Root.StructTreeRoot.K[-1].K.K = 1
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert check["metrics"]["excluded_described_words"] == 20


@pytest.mark.parametrize("tagged", [True, False])
@pytest.mark.parametrize("shadow_first", [False, True])
def test_coincident_unmarked_shadow_copy_requires_a_marked_counterpart(tmp_path, tagged, shadow_first):
    import pikepdf

    path = tagged_text_pages(tmp_path / "shadow.pdf", tagged_words=50, hidden_page=None if tagged else 1)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        shadow = page.Contents.read_bytes().replace(b"/P <</MCID 0>> BDC", b"/Artifact BMC")
        shadow = shadow.replace(b"54 650 Td", b"54.35 650 Td")
        # A near-identical size makes PDFium retain the shadow; it is still
        # coincident within half a point. Exact copies may be removed by PDFium.
        shadow = shadow.replace(b"10 Tf", b"10.001 Tf")
        original = page.Contents.read_bytes()
        page.Contents = pdf.make_stream(
            shadow + b"\n" + shadow.replace(b"10.001 Tf", b"10.002 Tf") + b"\n" + original
            if shadow_first else original + b"\n" + shadow
        )
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == ("pass" if tagged else "fail")
    if tagged:
        assert check["metrics"]["excluded_duplicate_characters"] > 0
        assert check["metrics"]["coverage"] == 1
    else:
        assert check["metrics"]["excluded_duplicate_characters"] == 0


def test_soft_hyphen_in_actualtext_does_not_split_a_word(tmp_path):
    path = tagged_text_pages(tmp_path / "soft-hyphen.pdf", tagged_words=1, actual_text="wo\u00adrd")
    report, _ = _assess_real(path, path)
    assert _check(report, "tagged_text_coverage")["metrics"]["coverage"] == 1


def test_headerless_table_size_boundary(tmp_path):
    for columns, rows, status in [(2, 2, "pass"), (2, 3, "fail"), (3, 2, "fail")]:
        path = tagged_table(tmp_path / f"size-{columns}-{rows}.pdf", headers=False, rows=rows, columns=columns)
        report, tasks = _assess_real(path, path)
        assert _check(report, "table_headers")["status"] == status
        assert any(t["blocking"] and t["task_type"] == "table_semantics" for t in tasks) is (status == "fail")


@pytest.mark.parametrize("properties", ["/Type /Pagination", "/Subtype /Header", "/Subtype /Footer"])
@pytest.mark.parametrize("form", [False, True], ids=["page-content", "form-invocation"])
def test_one_page_typed_pagination_artifact_exempts_margin_only(tmp_path, properties, form):
    import pikepdf

    from app.pipeline.tagger import _artifact_properties_for_element

    path = tagged_text_pages(tmp_path / "pagination.pdf", tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        # Include the exact named property dictionaries the tagger writes,
        # alongside the minimal accepted forms of the Pagination mark.
        page.Resources.Properties = pikepdf.Dictionary(
            RunningHead=_artifact_properties_for_element({"artifact_type": "page_header"}),
            RunningFoot=_artifact_properties_for_element({"artifact_type": "page_footer"}),
        )
        header = (
            f"/Artifact <<{properties}>> BDC BT /F1 10 Tf 54 760 Td "
            "(one two three four five six seven eight nine ten) Tj ET EMC "
            "/Artifact /RunningHead BDC BT /F1 10 Tf 54 740 Td (running head) Tj ET EMC "
            "/Artifact /RunningFoot BDC BT /F1 10 Tf 54 25 Td (page footer) Tj ET EMC"
        ).encode("ascii")
        if form:
            stream = pdf.make_stream(header)
            stream.Subtype = pikepdf.Name.Form
            stream.BBox = pikepdf.Array([0, 0, 612, 792])
            stream.Resources = pikepdf.Dictionary(Font=page.Resources.Font, Properties=page.Resources.Properties)
            page.Resources.XObject = pikepdf.Dictionary(Fx=stream)
            header = b"/Fx Do"
        page.Contents = pdf.make_stream(page.Contents.read_bytes() + b"\n" + header)
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert check["metrics"]["coverage"] == 1
    assert check["metrics"]["excluded_furniture_words"] == 14
    assert check["metrics"]["excluded_figure_words"] == 0

    untyped = tagged_text_pages(tmp_path / "untyped.pdf", running_head=True, tagged_words=3)
    report, _ = _assess_real(untyped, untyped)
    check = _check(report, "tagged_text_coverage")
    assert check["metrics"]["excluded_furniture_words"] == 0
    assert check["metrics"]["missing_words"] == 8

    mixed = tagged_text_pages(tmp_path / "mixed.pdf", pages=3, running_head=True, tagged_words=3)
    with pikepdf.open(mixed, allow_overwriting_input=True) as pdf:
        for page in list(pdf.pages)[:2]:
            page.Contents = pdf.make_stream(page.Contents.read_bytes().replace(
                b"/Artifact BMC", b"/Artifact <</Type /Pagination>> BDC",
            ))
        pdf.save(mixed)
    report, _ = _assess_real(mixed, mixed)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert check["metrics"]["excluded_furniture_words"] == 24

    rotated = tmp_path / "rotated.pdf"
    with pikepdf.open(path) as pdf:
        pdf.pages[0].Rotate = 90
        pdf.save(rotated)
    report, _ = _assess_real(rotated, rotated)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "warning"
    assert check["metrics"]["excluded_pagination_words"] == 0


@pytest.mark.parametrize("typed", [False, True], ids=["untyped", "pagination"])
def test_artifact_body_stays_eligible_with_trusted_margin_header(tmp_path, typed):
    import pikepdf

    path = tagged_text_pages(tmp_path / "hidden-body.pdf", hidden_page=1, tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        contents = page.Contents.read_bytes()
        if typed:
            contents = contents.replace(b"/Artifact BMC", b"/Artifact <</Type /Pagination /Subtype /Header>> BDC")
        header = b"/Artifact <</Type /Pagination>> BDC BT /F1 10 Tf 54 760 Td (running head) Tj ET EMC"
        page.Contents = pdf.make_stream(contents + b"\n" + header)
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "fail"
    assert check["metrics"]["eligible_words"] == 50
    assert check["metrics"]["missing_words"] == 50
    assert check["metrics"]["excluded_furniture_words"] == 2


@pytest.mark.parametrize("box_key", ["CropBox", "MediaBox"])
@pytest.mark.parametrize("rotation", [0, 90])
def test_coverage_excludes_words_outside_visible_page(tmp_path, box_key, rotation):
    import pikepdf

    path = tagged_text_pages(tmp_path / "cropped.pdf", tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page["/" + box_key] = pikepdf.Array([0, 540, 612, 792])
        page.Rotate = rotation
        outside = "/Artifact BMC BT /F1 10 Tf 14 TL 54 400 Td " + " ".join(
            "(cropped cropped cropped cropped cropped cropped cropped cropped cropped cropped) Tj T*"
            for _ in range(13)
        ) + " ET EMC"
        page.Contents = pdf.make_stream(page.Contents.read_bytes() + b"\n" + outside.encode("ascii"))
        pdf.save(path)
    report, tasks = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == "pass"
    assert check["metrics"]["eligible_words"] == 50
    assert check["metrics"]["excluded_off_page_words"] == 130
    assert not [t for t in tasks if t["metadata"].get("check") == "tagged_text_coverage"]


@pytest.mark.parametrize("bounds", ["BBox", "Layout", "marked-image", "clipped-form"])
@pytest.mark.parametrize("artifact", [True, False], ids=["artifact", "unreferenced-MCID"])
def test_coverage_exempts_only_artifact_words_inside_tagged_figure(tmp_path, bounds, artifact):
    import pikepdf

    path = tagged_text_pages(tmp_path / "figure-ocr.pdf", tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        picture = pdf.make_stream(b"\xff\xff\xff")
        picture.Type = pikepdf.Name.XObject
        picture.Subtype = pikepdf.Name.Image
        picture.Width = picture.Height = 1
        picture.ColorSpace = pikepdf.Name.DeviceRGB
        picture.BitsPerComponent = 8
        page.Resources.XObject = pikepdf.Dictionary(Im=picture)
        figure = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem, S=pikepdf.Name.Figure, Pg=page.obj, K=1,
        ))
        if bounds == "BBox":
            figure.BBox = pikepdf.Array([40, 200, 500, 430])
        elif bounds == "Layout":
            figure.A = pikepdf.Dictionary(O=pikepdf.Name.Layout, BBox=pikepdf.Array([40, 200, 500, 430]))
        if bounds == "clipped-form":
            # A full-page image clipped to one picture. Its raw image bounds
            # must not exempt the hidden body outside this clipped region.
            form = pdf.make_stream(b"/Figure <</MCID 1>> BDC q 10 100 230 115 re W n 306 0 0 396 0 0 cm /Im Do Q EMC")
            form.Subtype = pikepdf.Name.Form
            form.BBox = pikepdf.Array([0, 0, 306, 396])
            form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im=picture))
            page.Resources.XObject.Fx = form
            draw = b"q 2 0 0 2 20 0 cm /Fx Do Q"
            figure.K = pikepdf.Dictionary(Type=pikepdf.Name.MCR, MCID=1, Stm=form)
        else:
            draw = b"/Figure <</MCID 1>> BDC q 460 0 0 230 40 200 cm /Im Do Q EMC"
        mark = "/Artifact BMC" if artifact else "/P <</MCID 99>> BDC"
        noise = mark + " BT /F1 10 Tf 14 TL 54 400 Td " + " ".join(
            "(picture picture picture picture picture picture picture picture picture picture) Tj T*"
            for _ in range(13)
        ) + " ET EMC"
        page.Contents = pdf.make_stream(page.Contents.read_bytes() + b"\n" + draw + b"\n" + noise.encode("ascii"))
        pdf.Root.StructTreeRoot.K.append(figure)
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == ("pass" if artifact else "fail")
    assert check["metrics"]["eligible_words"] == (50 if artifact else 180)
    assert check["metrics"]["excluded_artifact_figure_words"] == (130 if artifact else 0)
    assert _check(report, "figure_descriptions")["status"] == "fail"

    if artifact:
        # The Figure's fallback rectangle must not swallow body outside it,
        # even when the marked image is a clipped full-page scan.
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            page = pdf.pages[0]
            page.Contents = pdf.make_stream(page.Contents.read_bytes().replace(
                b"/P <</MCID 0>> BDC", b"/Artifact BMC",
            ))
            pdf.Root.StructTreeRoot.K = pikepdf.Array([pdf.Root.StructTreeRoot.K[-1]])
            pdf.save(path)
        report, _ = _assess_real(path, path)
        check = _check(report, "tagged_text_coverage")
        assert check["status"] == "fail"
        assert check["metrics"]["missing_words"] == 50


@pytest.mark.parametrize(("share", "status"), [(0.59, "fail"), (0.61, "pass")])
def test_artifact_figure_exemption_requires_sixty_percent_word_overlap(tmp_path, share, status):
    import pikepdf

    from app.pipeline.pdfium_text import PdfiumText

    path = tagged_text_pages(tmp_path / "overlap.pdf", tagged_words=50)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        noise = "/Artifact BMC BT /F1 4 Tf 6 TL 350 700 Td " + " ".join(
            "(noise) Tj T*" for _ in range(100)
        ) + " ET EMC"
        page.Contents = pdf.make_stream(page.Contents.read_bytes() + b"\n" + noise.encode("ascii"))
        pdf.save(path)
    with PdfiumText(path) as text:
        _, (left, _, right, _) = next(w for w in text.words(0) if w[0] == "noise")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        figure = pdf.make_indirect(pikepdf.Dictionary(
            S=pikepdf.Name.Figure, Pg=pdf.pages[0].obj,
            BBox=pikepdf.Array([right - share * (right-left), 0, 612, 792]),
        ))
        pdf.Root.StructTreeRoot.K.append(figure)
        pdf.save(path)
    report, _ = _assess_real(path, path)
    check = _check(report, "tagged_text_coverage")
    assert check["status"] == status
    assert check["metrics"]["eligible_words"] == (50 if share >= .6 else 150)
