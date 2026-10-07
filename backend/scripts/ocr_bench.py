"""Produce olmOCR-Bench candidate outputs from the app's OCR stage.

Runs the production OCRmyPDF command (built by the app's own
``build_ocrmypdf_args``) on each single-page benchmark PDF, extracts the
resulting text layer with pdfminer, and writes the Markdown files the
olmOCR-Bench scorer expects:

    <bench_dir>/<candidate>/<subset>/<pdf stem>_pg1_repeat1.md

Score afterwards with ``scripts/score_olmocr.py``. For the Gateway engine,
pass ``--extra "--plugin app.pipeline.ocr_vlm_plugin"`` and the ``CAIL_OCR_*``
environment the OCR step sets (see ``app/pipeline/ocr_vlm_plugin.py``).

This measures the OCR text layer only. What a screen reader finally hears
also depends on Docling's layout and the tagger, which a full-pipeline run
measures separately.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdfminer.high_level import extract_text

from app.pipeline.ocr import build_ocrmypdf_args
from app.pipeline.pdf_repair import pdfminer_readable

DEFAULT_SUBSETS = ("old_scans", "long_tiny_text", "multi_column")


def ocr_args(pdf: Path, output: Path, language: str, extra: list[str]) -> list[str]:
    args = build_ocrmypdf_args(
        input_path=pdf,
        output_path=output,
        language=language,
        mode="skip",
        rotate_pages=True,
        deskew=True,
    )
    # Variants add flags before the input and output paths.
    return [*args[:-2], *extra, *args[-2:]]


def page_text(pdf: Path) -> str:
    """The text layer as pdfminer reads it, which is how the app's fidelity
    checks read it."""
    with pdfminer_readable(pdf) as readable:
        return extract_text(str(readable))


def convert(pdf: Path, target: Path, language: str, extra: list[str]) -> str:
    if target.exists():
        return "cached"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "out.pdf"
        result = subprocess.run(
            ocr_args(pdf, output, language, extra),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        # OCRmyPDF exit 6 means the page already had text and was skipped.
        if result.returncode not in (0, 6):
            # No output is written, so the next run retries the page.
            reason = (result.stderr.strip().splitlines() or [""])[-1]
            return f"exit {result.returncode}: {reason}"
        text = page_text(output if output.exists() else pdf)
    target.write_text(text, encoding="utf-8")
    return f"exit {result.returncode}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-dir", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--subsets", nargs="+", default=list(DEFAULT_SUBSETS))
    parser.add_argument("--language", default="eng")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--extra",
        default="",
        help="Additional OCRmyPDF flags, as one space-separated string",
    )
    options = parser.parse_args()
    extra = options.extra.split()

    jobs = []
    for subset in options.subsets:
        for pdf in sorted((options.bench_dir / "pdfs" / subset).glob("*.pdf")):
            target = options.bench_dir / options.candidate / subset / f"{pdf.stem}_pg1_repeat1.md"
            jobs.append((pdf, target))

    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        results = pool.map(
            lambda job: (job[0], convert(job[0], job[1], options.language, extra)),
            jobs,
        )
        for pdf, status in results:
            print(f"{pdf.parent.name}/{pdf.name}: {status}", flush=True)


if __name__ == "__main__":
    main()
