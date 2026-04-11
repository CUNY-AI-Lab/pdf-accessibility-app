from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import adobe_accessibility_check as adobe_check


def _credentials_payload() -> dict:
    return {
        "client_credentials": {
            "client_id": "x" * 32,
            "client_secret": "y" * 36,
        },
        "service_principal_credentials": {
            "organization_id": "z" * 33,
        },
    }


def test_load_credentials_from_zip_without_persisting_secrets(tmp_path: Path) -> None:
    credentials_zip = tmp_path / "PDFServicesAPI-Credentials.zip"
    with zipfile.ZipFile(credentials_zip, "w") as archive:
        archive.writestr("pdfservices-api-credentials.json", json.dumps(_credentials_payload()))

    credentials = adobe_check._load_credentials(credentials_zip)

    assert credentials.client_id == "x" * 32
    assert credentials.client_secret == "y" * 36


def test_quota_guard_blocks_when_local_cap_would_be_exceeded(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    ledger.write_text(json.dumps({"months": {"2026-04": {"transactions": 100}}}))

    with pytest.raises(RuntimeError, match="local quota guard blocked"):
        adobe_check._assert_monthly_quota(
            ledger,
            month="2026-04",
            monthly_limit=100,
            planned=1,
        )


def test_record_usage_tracks_monthly_transaction_without_repo_state(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"

    adobe_check._record_usage(
        ledger,
        month="2026-04",
        pdf_path=Path("/tmp/input.pdf"),
        report_path=Path("/tmp/report.json"),
        result_path=Path("/tmp/result.pdf"),
        now=datetime(2026, 4, 11, tzinfo=timezone.utc),
    )

    data = json.loads(ledger.read_text())
    assert data["months"]["2026-04"]["transactions"] == 1
    assert data["months"]["2026-04"]["runs"][0]["pdf"] == "/tmp/input.pdf"


def test_count_statuses_recurses_through_report_shape() -> None:
    report = {
        "overall": {"status": "Passed"},
        "checks": [
            {"result": "Failed"},
            {"state": "Needs Manual Check"},
            {"status": "passed"},
        ],
    }

    assert adobe_check._count_statuses(report) == {
        "failed": 1,
        "needs manual check": 1,
        "passed": 2,
    }


def test_main_dry_run_requires_no_adobe_sdk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    credentials_json = tmp_path / "credentials.json"
    credentials_json.write_text(json.dumps(_credentials_payload()))
    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n%%EOF\n")
    ledger = tmp_path / "usage.json"

    result = adobe_check.main(
        [
            str(pdf_path),
            "--credentials",
            str(credentials_json),
            "--quota-ledger",
            str(ledger),
            "--monthly-limit",
            "1",
            "--dry-run",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["local_transactions_after_run"] == 1
    assert not ledger.exists()
