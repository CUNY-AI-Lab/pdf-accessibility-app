"""The veraPDF report parser.

The JSON reports under verapdf_reports/ are real veraPDF 1.28.0 output (core and
validation model 1.28.2, the production version) for a minimal PDF/UA-1 file, an
untagged sample PDF, and a file veraPDF could not parse. Only the local path in
itemDetails.name was shortened.
"""

import json
from pathlib import Path

import pytest

from app.pipeline.validator import parse_verapdf_report, validate_pdf

REPORTS = Path(__file__).parent / "verapdf_reports"


def _report(name: str) -> dict:
    return json.loads((REPORTS / name).read_text())


def test_compliant_report_is_compliant():
    result = parse_verapdf_report(_report("compliant.json"))

    assert result.compliant is True
    assert result.violations == []
    assert result.error is None


def test_noncompliant_report_lists_failed_rules():
    result = parse_verapdf_report(_report("noncompliant.json"))

    assert result.compliant is False
    rule_ids = {violation.rule_id for violation in result.violations}
    assert "ISO 14289-1:2014-7.1-11" in rule_ids
    font_rule = next(v for v in result.violations if v.rule_id == "ISO 14289-1:2014-7.21.4.1-1")
    assert font_rule.severity == "error"
    assert font_rule.count == 2
    assert font_rule.category == "fonts"


def test_task_exception_is_not_compliant():
    result = parse_verapdf_report(_report("task_exception.json"))

    assert result.compliant is False
    assert result.violations == []
    assert "Couldn't parse stream" in (result.error or "")


@pytest.mark.parametrize(
    "report",
    [
        {"report": {"jobs": []}},
        {"report": {}},
        {},
        [],
        {"report": {"jobs": [{"itemDetails": {"name": "x.pdf"}}]}},
        {"report": {"jobs": [{"validationResult": []}]}},
        {"report": {"jobs": [{"validationResult": [{"details": {"ruleSummaries": []}}]}]}},
        {"report": {"jobs": [{"validationResult": [{"compliant": "true"}]}]}},
    ],
    ids=[
        "empty-jobs",
        "no-jobs-key",
        "no-report",
        "not-an-object",
        "job-without-result",
        "empty-result-list",
        "result-without-verdict",
        "verdict-not-boolean",
    ],
)
def test_report_without_a_compliant_verdict_is_not_compliant(report):
    result = parse_verapdf_report(report)

    assert result.compliant is False
    assert result.error


def test_validation_that_ended_early_is_not_compliant():
    report = _report("compliant.json")
    report["report"]["jobs"][0]["validationResult"][0]["jobEndStatus"] = "timeout"

    result = parse_verapdf_report(report)

    assert result.compliant is False
    assert "timeout" in (result.error or "")


def test_any_noncompliant_job_makes_the_report_noncompliant():
    compliant_job = _report("compliant.json")["report"]["jobs"][0]
    noncompliant_job = _report("noncompliant.json")["report"]["jobs"][0]
    report = {"report": {"jobs": [compliant_job, noncompliant_job]}}

    assert parse_verapdf_report(report).compliant is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "exit_code", "expected"),
    [
        ('{"report":{"jobs":[]}}', 0, False),
        ((REPORTS / "compliant.json").read_text(), 0, True),
    ],
    ids=["empty-report-exit-0", "compliant-report"],
)
async def test_validate_pdf_reads_the_verdict_from_the_report(
    tmp_path, stdout, exit_code, expected
):
    report_file = tmp_path / "report.json"
    report_file.write_text(stdout)
    verapdf = tmp_path / "verapdf"
    verapdf.write_text(f"#!/bin/sh\ncat '{report_file}'\nexit {exit_code}\n")
    verapdf.chmod(0o755)
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")

    result = await validate_pdf(pdf, verapdf_path=str(verapdf))

    assert result.compliant is expected
