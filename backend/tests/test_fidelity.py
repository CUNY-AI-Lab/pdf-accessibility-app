import logging
from pathlib import Path

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
    assert form_check["status"] == "fail"
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


def test_scanned_fidelity_fails_when_visible_scan_has_no_ocr_text_or_structure(monkeypatch):
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
        input_pdf=Path("scan.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="scanned",
    )

    assert report["passed"] is False
    assert any(
        check["check"] == "ocr_coverage" and check["status"] == "fail"
        for check in report["checks"]
    )
    assert any(
        task["task_type"] == "content_fidelity" and task["blocking"]
        for task in tasks
    )


def test_scanned_fidelity_skips_ocr_coverage_for_visually_blank_pages(monkeypatch):
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
        output_pdf=Path("out.pdf"),
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


def test_fidelity_uses_comparison_source_for_ocr_rescue(monkeypatch):
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
        output_pdf=Path("out.pdf"),
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


def test_fidelity_allows_advisory_machine_alt_without_blocking(monkeypatch):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
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


def test_fidelity_flags_large_text_drift(monkeypatch):
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
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": []},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    assert any(task["task_type"] == "content_fidelity" and task["blocking"] for task in tasks)


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


def test_fidelity_does_not_block_on_single_reading_order_outlier(monkeypatch):
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
        output_pdf=Path("out.pdf"),
        structure_json={"elements": [{"type": "heading", "text": text} for text in fragments]},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is True
    assert not any(task["task_type"] == "reading_order" and task["blocking"] for task in tasks)


def test_fidelity_blocks_truly_scrambled_reading_order(monkeypatch):
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
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json={"elements": [{"type": "heading", "text": text} for text in fragments]},
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 0},
        classification="digital",
    )

    assert report["passed"] is False
    assert any(task["task_type"] == "reading_order" and task["blocking"] for task in tasks)


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


def test_fidelity_flags_high_risk_complex_tables(monkeypatch):
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
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
        structure_json=structure,
        alt_entries=[],
        validation_report=_validation_report(compliant=True, violations=[]),
        tagging_metrics={"tables_tagged": 1},
        classification="digital",
    )

    assert report["passed"] is False
    task = next(task for task in tasks if task["task_type"] == "table_semantics")
    assert task["blocking"] is True
    assert task["source"] == "fidelity"
    assert task["metadata"]["complex_tables"] == 1
    assert task["metadata"]["high_risk_tables"] == 1
    assert task["metadata"]["pages_to_check"] == [1]
    assert task["metadata"]["table_review_targets"][0]["table_review_id"] == "review-0"


def test_fidelity_blocks_when_detected_figures_are_not_tagged(monkeypatch):
    monkeypatch.setattr(fidelity, "_extract_pdf_text_sample", lambda path: "sample text " * 50)
    monkeypatch.setattr(fidelity, "_sample_visual_ink", lambda path: {
        "sampled_pages": 1,
        "pages_with_visible_ink": 1,
        "mean_ink_ratio": 0.01,
        "max_ink_ratio": 0.01,
        "visually_blank": False,
    })

    report, tasks = assess_fidelity(
        input_pdf=Path("in.pdf"),
        output_pdf=Path("out.pdf"),
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

    assert report["passed"] is False
    figure_check = next(check for check in report["checks"] if check["check"] == "figure_coverage")
    assert figure_check["status"] == "warning"
    assert figure_check["metrics"]["coverage"] == 0.0
    task = next(task for task in tasks if task["task_type"] == "figure_semantics")
    assert task["blocking"] is True
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
