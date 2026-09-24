"""Produce olmOCR-Bench candidate outputs from the app's OCR stage.

Runs the production OCRmyPDF command (built by the app's own
``_build_ocrmypdf_args``) on each single-page benchmark PDF, extracts the
resulting text layer with pdfminer, and writes the Markdown files the
olmOCR-Bench scorer expects:

    <bench_dir>/<candidate>/<subset>/<pdf stem>_pg1_repeat1.md

Score afterwards with ``python -m olmocr.bench.benchmark --dir <bench_dir>``.

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

from app.pipeline.ocr import _build_ocrmypdf_args

DEFAULT_SUBSETS = ("old_scans", "long_tiny_text", "multi_column")


def ocr_args(pdf: Path, output: Path, language: str, extra: list[str]) -> list[str]:
    args = _build_ocrmypdf_args(
        input_path=pdf,
        output_path=output,
        language=language,
        mode="skip",
        rotate_pages=True,
        deskew=True,
    )
    # Variants add flags before the input and output paths.
    return [*args[:-2], *extra, *args[-2:]]


def page_text(pdf: Path) -> tuple[str, str]:
    """The text layer, via pdfminer (what the app's fidelity checks read), or
    Poppler's pdftotext when pdfminer cannot parse the file."""
    try:
        return extract_text(str(pdf)), "pdfminer"
    except Exception:  # noqa: BLE001 - any parser failure falls back
        result = subprocess.run(
            ["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=False
        )
        return result.stdout, "pdftotext"


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
        text, extractor = page_text(output if output.exists() else pdf)
        if result.returncode not in (0, 6):
            text = ""
    target.write_text(text, encoding="utf-8")
    return f"exit {result.returncode} ({extractor})"


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
            target = (
                options.bench_dir
                / options.candidate
                / subset
                / f"{pdf.stem}_pg1_repeat1.md"
            )
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
