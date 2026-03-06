from pathlib import Path

from app.pipeline import fidelity
from app.pipeline.fidelity import _reading_order_metrics, assess_fidelity


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
