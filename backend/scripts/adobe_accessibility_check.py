from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MONTHLY_LIMIT = 100
DEFAULT_LEDGER_PATH = (
    Path.home() / ".cache" / "pdf-accessibility-app" / "adobe_accessibility_usage.json"
)


@dataclass(frozen=True)
class AdobeCredentials:
    client_id: str
    client_secret: str


def _current_month(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    return f"{value.year:04d}-{value.month:02d}"


def _load_credentials(path: Path) -> AdobeCredentials:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".json") and not name.endswith("/")
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"Expected exactly one JSON credentials file in {path}, found {len(candidates)}."
                )
            with archive.open(candidates[0]) as handle:
                raw = json.load(handle)
    else:
        raw = json.loads(path.read_text())

    client_credentials = raw.get("client_credentials")
    if not isinstance(client_credentials, dict):
        raise ValueError("Credentials file is missing client_credentials.")

    client_id = str(client_credentials.get("client_id") or "").strip()
    client_secret = str(client_credentials.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Credentials file is missing client_id or client_secret.")

    return AdobeCredentials(client_id=client_id, client_secret=client_secret)


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"months": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Usage ledger is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Usage ledger must contain a JSON object: {path}")
    months = data.setdefault("months", {})
    if not isinstance(months, dict):
        raise ValueError(f"Usage ledger months must be an object: {path}")
    return data


def _ledger_usage(path: Path, month: str) -> int:
    data = _read_ledger(path)
    entry = data["months"].get(month, {})
    if not isinstance(entry, dict):
        return 0
    return int(entry.get("transactions", 0) or 0)


def _assert_monthly_quota(path: Path, *, month: str, monthly_limit: int, planned: int) -> None:
    used = _ledger_usage(path, month)
    if used + planned > monthly_limit:
        raise RuntimeError(
            "Adobe checker local quota guard blocked this run: "
            f"{used} used + {planned} planned > {monthly_limit} local cap for {month}. "
            "Raise --monthly-limit only if you intentionally want to spend more of the Adobe quota."
        )


def _record_usage(
    path: Path,
    *,
    month: str,
    pdf_path: Path,
    report_path: Path,
    result_path: Path,
    now: datetime | None = None,
) -> None:
    data = _read_ledger(path)
    months = data.setdefault("months", {})
    entry = months.setdefault(month, {"transactions": 0, "runs": []})
    if not isinstance(entry, dict):
        entry = {"transactions": 0, "runs": []}
        months[month] = entry
    runs = entry.setdefault("runs", [])
    if not isinstance(runs, list):
        runs = []
        entry["runs"] = runs
    entry["transactions"] = int(entry.get("transactions", 0) or 0) + 1
    runs.append(
        {
            "timestamp": (now or datetime.now(UTC)).isoformat(),
            "pdf": str(pdf_path),
            "report": str(report_path),
            "result_pdf": str(result_path),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _count_statuses(value: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"status", "result", "state"} and isinstance(item, str):
                counter[item.strip().lower()] += 1
            counter.update(_count_statuses(item))
    elif isinstance(value, list):
        for item in value:
            counter.update(_count_statuses(item))
    return counter


def _report_summary(report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text())
    return {
        "top_level_keys": sorted(report) if isinstance(report, dict) else [],
        "status_counts": dict(sorted(_count_statuses(report).items())),
    }


def _run_adobe_checker(
    *,
    pdf_path: Path,
    output_dir: Path,
    credentials: AdobeCredentials,
) -> tuple[Path, Path]:
    try:
        from adobe.pdfservices.operation.auth.service_principal_credentials import (
            ServicePrincipalCredentials,
        )
        from adobe.pdfservices.operation.exception.exceptions import (
            SdkException,
            ServiceApiException,
            ServiceUsageException,
        )
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
        from adobe.pdfservices.operation.pdfjobs.jobs.pdf_accessibility_checker_job import (
            PDFAccessibilityCheckerJob,
        )
        from adobe.pdfservices.operation.pdfjobs.result.pdf_accessibility_checker_result import (
            PDFAccessibilityCheckerResult,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing Adobe PDF Services SDK. Run with: "
            "uv run --with pdfservices-sdk python backend/scripts/adobe_accessibility_check.py ..."
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    result_pdf = output_dir / f"{pdf_path.stem}.adobe-accessibility.pdf"
    report_json = output_dir / f"{pdf_path.stem}.adobe-accessibility.json"

    sdk_credentials = ServicePrincipalCredentials(
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
    )
    pdf_services = PDFServices(credentials=sdk_credentials)

    try:
        input_asset = pdf_services.upload(
            input_stream=pdf_path.read_bytes(),
            mime_type=PDFServicesMediaType.PDF,
        )
        job = PDFAccessibilityCheckerJob(input_asset=input_asset)
        location = pdf_services.submit(job)
        response = pdf_services.get_job_result(location, PDFAccessibilityCheckerResult)

        result = response.get_result()
        stream_asset = pdf_services.get_content(result.get_asset())
        stream_report = pdf_services.get_content(result.get_report())
    except (ServiceApiException, ServiceUsageException, SdkException) as exc:
        raise RuntimeError(f"Adobe Accessibility Checker failed: {exc}") from exc

    result_pdf.write_bytes(stream_asset.get_input_stream())
    report_json.write_bytes(stream_report.get_input_stream())
    return result_pdf, report_json


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Adobe PDF Accessibility Checker API for local benchmark validation only. "
            "This uploads the PDF to Adobe and consumes one Adobe document transaction."
        )
    )
    parser.add_argument("pdf", type=Path, help="PDF to check.")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=os.getenv("ADOBE_PDF_SERVICES_CREDENTIALS"),
        help="Path to Adobe credentials JSON or PDFServicesAPI-Credentials.zip.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/adobe-accessibility-checks"),
        help="Directory for Adobe result PDF and JSON report. Default is under ignored data/.",
    )
    parser.add_argument(
        "--quota-ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help=f"Local usage ledger. Default: {DEFAULT_LEDGER_PATH}",
    )
    parser.add_argument(
        "--monthly-limit",
        type=int,
        default=DEFAULT_MONTHLY_LIMIT,
        help=(
            "Local monthly transaction cap for this script. "
            f"Default {DEFAULT_MONTHLY_LIMIT}, intentionally below Adobe's account limit."
        ),
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="Required to actually consume one Adobe document transaction.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and quota guard without calling Adobe.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    pdf_path = args.pdf.expanduser().resolve()
    credentials_path = Path(args.credentials).expanduser().resolve() if args.credentials else None
    output_dir = args.output_dir.expanduser().resolve()
    quota_ledger = args.quota_ledger.expanduser().resolve()
    month = _current_month()

    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    if credentials_path is None or not credentials_path.exists():
        print("Adobe credentials file not found. Pass --credentials.", file=sys.stderr)
        return 2
    if args.monthly_limit < 1:
        print("--monthly-limit must be at least 1.", file=sys.stderr)
        return 2

    credentials = _load_credentials(credentials_path)
    _assert_monthly_quota(
        quota_ledger,
        month=month,
        monthly_limit=args.monthly_limit,
        planned=1,
    )

    used = _ledger_usage(quota_ledger, month)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "pdf": str(pdf_path),
                    "month": month,
                    "local_monthly_limit": args.monthly_limit,
                    "local_transactions_used": used,
                    "local_transactions_after_run": used + 1,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not args.confirm_spend:
        print(
            "This will upload the PDF to Adobe and consume one document transaction. "
            "Re-run with --confirm-spend to proceed.",
            file=sys.stderr,
        )
        return 2

    result_pdf, report_json = _run_adobe_checker(
        pdf_path=pdf_path,
        output_dir=output_dir,
        credentials=credentials,
    )
    _record_usage(
        quota_ledger,
        month=month,
        pdf_path=pdf_path,
        report_path=report_json,
        result_path=result_pdf,
    )
    summary = _report_summary(report_json)
    print(
        json.dumps(
            {
                "pdf": str(pdf_path),
                "result_pdf": str(result_pdf),
                "report_json": str(report_json),
                "quota_ledger": str(quota_ledger),
                "month": month,
                "local_monthly_limit": args.monthly_limit,
                "local_transactions_used": used + 1,
                "days_in_month": calendar.monthrange(
                    int(month.split("-")[0]), int(month.split("-")[1])
                )[1],
                **summary,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
