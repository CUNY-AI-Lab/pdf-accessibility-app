"""Adobe baseline for the OCR and tagging benchmarks.

Runs Adobe PDF Services on benchmark PDFs the way Acrobat Pro remediates a
scan: the OCR operation (searchable image), then Auto-Tag. The tagged PDF is
saved as ``<bench_dir>/<candidate>/<subset>/<stem>.tagged.pdf``, which the
scorers read, with Adobe's Auto-Tag report beside it. A PDF with a tagged
result is skipped, so a run can be resumed.

Adobe counts OCR as one document transaction for up to 50 pages and Auto-Tag
as ten per page. So each subset's pages are joined for OCR, at most 50 to a
call, and the recognized pages are split apart again and tagged one by one;
OCR works page by page, so this changes only the count. Transactions are
recorded as Adobe counts them, in the same local ledger as
``adobe_accessibility_check.py``, under a cap of the free tier's 500 a month.
Nothing is sent without ``--confirm-spend``. Run with
``uv run --with pdfservices-sdk``.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
from pathlib import Path

import pikepdf
from adobe_accessibility_check import (
    DEFAULT_LEDGER_PATH,
    AdobeCredentials,
    _assert_monthly_quota,
    _current_month,
    _load_credentials,
    _record_usage,
)

FREE_TIER_MONTHLY_TRANSACTIONS = 500
OCR_PAGES_PER_TRANSACTION = 50
AUTOTAG_TRANSACTIONS_PER_PAGE = 10


def planned_transactions(pages_by_subset: dict[str, int]) -> int:
    return sum(
        math.ceil(pages / OCR_PAGES_PER_TRANSACTION) + AUTOTAG_TRANSACTIONS_PER_PAGE * pages
        for pages in pages_by_subset.values()
    )


class Adobe:
    def __init__(self, credentials: AdobeCredentials) -> None:
        from adobe.pdfservices.operation.auth.service_principal_credentials import (
            ServicePrincipalCredentials,
        )
        from adobe.pdfservices.operation.pdf_services import PDFServices

        self.services = PDFServices(
            credentials=ServicePrincipalCredentials(
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
            )
        )

    def _upload(self, data: bytes):
        from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType

        return self.services.upload(input_stream=data, mime_type=PDFServicesMediaType.PDF)

    def ocr(self, data: bytes) -> bytes:
        from adobe.pdfservices.operation.pdfjobs.jobs.ocr_pdf_job import OCRPDFJob
        from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_params import (
            OCRParams,
            OCRSupportedType,
        )
        from adobe.pdfservices.operation.pdfjobs.result.ocr_pdf_result import OCRPDFResult

        job = OCRPDFJob(
            self._upload(data),
            ocr_pdf_params=OCRParams(ocr_type=OCRSupportedType.SEARCHABLE_IMAGE),
        )
        result = self.services.get_job_result(self.services.submit(job), OCRPDFResult)
        return self.services.get_content(result.get_result().get_asset()).get_input_stream()

    def autotag(self, data: bytes) -> tuple[bytes, bytes]:
        """The tagged PDF and Adobe's report."""
        from adobe.pdfservices.operation.pdfjobs.jobs.autotag_pdf_job import AutotagPDFJob
        from adobe.pdfservices.operation.pdfjobs.params.autotag_pdf.autotag_pdf_params import (
            AutotagPDFParams,
        )
        from adobe.pdfservices.operation.pdfjobs.result.autotag_pdf_result import AutotagPDFResult

        job = AutotagPDFJob(
            self._upload(data), autotag_pdf_params=AutotagPDFParams(generate_report=True)
        )
        result = self.services.get_job_result(self.services.submit(job), AutotagPDFResult)
        tagged = result.get_result()
        return (
            self.services.get_content(tagged.get_tagged_pdf()).get_input_stream(),
            self.services.get_content(tagged.get_report()).get_input_stream(),
        )


def joined(pdfs: list[Path]) -> bytes:
    with pikepdf.new() as out:
        for pdf in pdfs:
            with pikepdf.open(pdf) as source:
                out.pages.extend(source.pages)
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()


def split(data: bytes) -> list[bytes]:
    pages = []
    with pikepdf.open(io.BytesIO(data)) as source:
        for page in source.pages:
            with pikepdf.new() as out:
                out.pages.append(page)
                buffer = io.BytesIO()
                out.save(buffer)
                pages.append(buffer.getvalue())
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--candidate", default="adobe_ocr_autotag")
    parser.add_argument("--subsets", nargs="+", required=True)
    parser.add_argument("--pages", type=int, help="run at most this many pages per subset")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=os.getenv("ADOBE_PDF_SERVICES_CREDENTIALS"),
        help="Adobe credentials JSON or PDFServicesAPI-Credentials.zip.",
    )
    parser.add_argument("--quota-ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--monthly-limit", type=int, default=FREE_TIER_MONTHLY_TRANSACTIONS)
    parser.add_argument("--confirm-spend", action="store_true")
    options = parser.parse_args()

    jobs: dict[str, list[Path]] = {}
    for subset in options.subsets:
        out_dir = options.bench_dir / options.candidate / subset
        pending = [
            pdf
            for pdf in sorted((options.bench_dir / "pdfs" / subset).glob("*.pdf"))
            if not (out_dir / f"{pdf.stem}.tagged.pdf").exists()
        ]
        jobs[subset] = pending[: options.pages]

    month = _current_month()
    planned = planned_transactions({subset: len(pdfs) for subset, pdfs in jobs.items()})
    _assert_monthly_quota(
        options.quota_ledger, month=month, monthly_limit=options.monthly_limit, planned=planned
    )
    print(f"{sum(map(len, jobs.values()))} pages to run, {planned} Adobe transactions", flush=True)
    if not options.confirm_spend:
        print("Dry run: pass --confirm-spend to call Adobe.")
        return 0
    if options.credentials is None:
        print("No credentials: pass --credentials or set ADOBE_PDF_SERVICES_CREDENTIALS.")
        return 2
    adobe = Adobe(_load_credentials(Path(options.credentials).expanduser()))

    for subset, pdfs in jobs.items():
        out_dir = options.bench_dir / options.candidate / subset
        out_dir.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(pdfs), OCR_PAGES_PER_TRANSACTION):
            batch = pdfs[start : start + OCR_PAGES_PER_TRANSACTION]
            try:
                recognized = split(adobe.ocr(joined(batch)))
            except Exception as exc:  # noqa: BLE001 - report and continue the batch
                print(f"{subset}: Adobe OCR failed for {len(batch)} pages: {exc}", flush=True)
                continue
            _record_usage(
                options.quota_ledger,
                month=month,
                pdf_path=batch[0],
                report_path=out_dir,
                result_path=out_dir,
                transactions=1,
            )
            for pdf, page in zip(batch, recognized, strict=True):
                tagged = out_dir / f"{pdf.stem}.tagged.pdf"
                report = out_dir / f"{pdf.stem}.autotag-report.xlsx"
                try:
                    tagged_bytes, report_bytes = adobe.autotag(page)
                except Exception as exc:  # noqa: BLE001 - report and continue the batch
                    print(f"{subset}/{pdf.name}: Adobe Auto-Tag failed: {exc}", flush=True)
                    continue
                _record_usage(
                    options.quota_ledger,
                    month=month,
                    pdf_path=pdf,
                    report_path=report,
                    result_path=tagged,
                    transactions=AUTOTAG_TRANSACTIONS_PER_PAGE,
                )
                tagged.write_bytes(tagged_bytes)
                report.write_bytes(report_bytes)
                print(f"{subset}/{pdf.name}: tagged", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
