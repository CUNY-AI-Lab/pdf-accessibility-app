"""Adobe baseline for the OCR and tagging benchmarks.

For each benchmark PDF, runs Adobe PDF Services the way Acrobat Pro remediates
a scan: the OCR operation (searchable image), then Auto-Tag. The tagged PDF is
read as a screen reader would (``app/services/structure_text.py``) and written
where the olmOCR-Bench scorer expects it:

    <bench_dir>/<candidate>/<subset>/<pdf stem>_pg1_repeat1.md

Each page costs two Adobe document transactions, recorded in the same local
ledger as ``adobe_accessibility_check.py``. Nothing is sent without
``--confirm-spend``. Run with ``uv run --with pdfservices-sdk``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from adobe_accessibility_check import (
    DEFAULT_LEDGER_PATH,
    AdobeCredentials,
    _assert_monthly_quota,
    _current_month,
    _load_credentials,
    _record_usage,
)

from app.services.structure_text import screen_reader_text

TRANSACTIONS_PER_PAGE = 2


def adobe_remediate(pdf: Path, out_dir: Path, credentials: AdobeCredentials) -> tuple[Path, Path]:
    """OCR then Auto-Tag one PDF; returns the tagged PDF and Adobe's report."""
    from adobe.pdfservices.operation.auth.service_principal_credentials import (
        ServicePrincipalCredentials,
    )
    from adobe.pdfservices.operation.pdf_services import PDFServices
    from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
    from adobe.pdfservices.operation.pdfjobs.jobs.autotag_pdf_job import AutotagPDFJob
    from adobe.pdfservices.operation.pdfjobs.jobs.ocr_pdf_job import OCRPDFJob
    from adobe.pdfservices.operation.pdfjobs.params.autotag_pdf.autotag_pdf_params import (
        AutotagPDFParams,
    )
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_params import (
        OCRParams,
        OCRSupportedType,
    )
    from adobe.pdfservices.operation.pdfjobs.result.autotag_pdf_result import AutotagPDFResult
    from adobe.pdfservices.operation.pdfjobs.result.ocr_pdf_result import OCRPDFResult

    services = PDFServices(
        credentials=ServicePrincipalCredentials(
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
        )
    )
    uploaded = services.upload(input_stream=pdf.read_bytes(), mime_type=PDFServicesMediaType.PDF)
    ocr_job = OCRPDFJob(
        uploaded,
        ocr_pdf_params=OCRParams(ocr_type=OCRSupportedType.SEARCHABLE_IMAGE),
    )
    ocr_result = services.get_job_result(services.submit(ocr_job), OCRPDFResult).get_result()
    tag_job = AutotagPDFJob(
        ocr_result.get_asset(),
        autotag_pdf_params=AutotagPDFParams(generate_report=True),
    )
    tag_result = services.get_job_result(services.submit(tag_job), AutotagPDFResult).get_result()

    tagged = out_dir / f"{pdf.stem}.tagged.pdf"
    report = out_dir / f"{pdf.stem}.autotag-report.xlsx"
    tagged.write_bytes(services.get_content(tag_result.get_tagged_pdf()).get_input_stream())
    report.write_bytes(services.get_content(tag_result.get_report()).get_input_stream())
    return tagged, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--candidate", default="adobe_ocr_autotag")
    parser.add_argument("--subsets", nargs="+", required=True)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=os.getenv("ADOBE_PDF_SERVICES_CREDENTIALS"),
        help="Adobe credentials JSON or PDFServicesAPI-Credentials.zip.",
    )
    parser.add_argument("--quota-ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--monthly-limit", type=int, default=450)
    parser.add_argument("--confirm-spend", action="store_true")
    options = parser.parse_args()

    jobs = []
    for subset in options.subsets:
        out_dir = options.bench_dir / options.candidate / subset
        for pdf in sorted((options.bench_dir / "pdfs" / subset).glob("*.pdf")):
            target = out_dir / f"{pdf.stem}_pg1_repeat1.md"
            if not target.exists():
                jobs.append((pdf, out_dir, target))

    month = _current_month()
    planned = TRANSACTIONS_PER_PAGE * len(jobs)
    _assert_monthly_quota(
        options.quota_ledger, month=month, monthly_limit=options.monthly_limit, planned=planned
    )
    print(f"{len(jobs)} pages to run, {planned} Adobe transactions", flush=True)
    if not options.confirm_spend:
        print("Dry run: pass --confirm-spend to call Adobe.")
        return 0
    if options.credentials is None:
        print("No credentials: pass --credentials or set ADOBE_PDF_SERVICES_CREDENTIALS.")
        return 2
    credentials = _load_credentials(Path(options.credentials).expanduser())

    for pdf, out_dir, target in jobs:
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            tagged, report = adobe_remediate(pdf, out_dir, credentials)
        except Exception as exc:  # noqa: BLE001 - record and continue the batch
            print(f"{pdf.parent.name}/{pdf.name}: Adobe failed: {exc}", flush=True)
            continue
        for _ in range(TRANSACTIONS_PER_PAGE):
            _record_usage(
                options.quota_ledger,
                month=month,
                pdf_path=pdf,
                report_path=report,
                result_path=tagged,
            )
        text = screen_reader_text(tagged)
        target.write_text(text, encoding="utf-8")
        print(f"{pdf.parent.name}/{pdf.name}: {len(text.split())} words", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
