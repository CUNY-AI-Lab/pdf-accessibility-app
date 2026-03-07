from pathlib import Path

from app.pipeline import fidelity
from app.pipeline.fidelity import (
    _extract_font_review_targets,
    _reading_order_metrics,
    assess_fidelity,
)


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
    with pikepdf.open("backend/test_sample.pdf") as pdf:
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
        tagging_metrics={"tables_tagged": 0},
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
