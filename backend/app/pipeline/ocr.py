"""Step 2: OCR scanned PDFs using OCRmyPDF."""

import asyncio
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from app.config import get_settings
from app.pipeline.subprocess_utils import (
    SubprocessTimeout,
    communicate_with_timeout,
    subprocess_process_group_kwargs,
)
from app.services.llm_client import current_model_endpoint
from app.services.runtime_paths import enriched_subprocess_env

logger = logging.getLogger(__name__)

GATEWAY_OCR_PLUGIN = "app.pipeline.ocr_vlm_plugin"
# Time for Tesseract to read a page after the Gateway gave up on it.
GATEWAY_PAGE_FALLBACK_SECONDS = 60


@dataclass
class OcrResult:
    success: bool
    output_path: Path
    skipped: bool = False
    message: str = ""


def build_ocrmypdf_args(
    *,
    input_path: Path,
    output_path: Path,
    language: str,
    mode: str,
    rotate_pages: bool,
    deskew: bool,
    jobs: int | None = None,
    max_image_mpixels: int | None = None,
    engine: str = "tesseract",
    pages: list[int] | None = None,
) -> list[str]:
    settings = get_settings()
    default_jobs = settings.ocr_gateway_jobs if engine == "gateway" else settings.ocrmypdf_jobs
    try:
        resolved_jobs = int(jobs if jobs is not None else default_jobs)
    except (TypeError, ValueError):
        resolved_jobs = 1
    try:
        resolved_max_image_mpixels = int(
            max_image_mpixels
            if max_image_mpixels is not None
            else settings.ocrmypdf_max_image_mpixels
        )
    except (TypeError, ValueError):
        resolved_max_image_mpixels = 75

    args = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--language",
        language,
        "--output-type",
        "pdf",
        "--jobs",
        str(max(1, resolved_jobs)),
        # Keep Pillow's decompression guard low enough that high-DPI scans fail
        # cleanly instead of expanding until the app/container is killed.
        "--max-image-mpixels",
        str(max(1, resolved_max_image_mpixels)),
    ]
    if engine == "gateway":
        args.extend(["--plugin", GATEWAY_OCR_PLUGIN])
    if mode == "redo":
        if rotate_pages:
            args.append("--rotate-pages")
        args.append("--redo-ocr")
    elif mode == "force":
        if rotate_pages:
            args.append("--rotate-pages")
        if deskew:
            args.append("--deskew")
        args.append("--force-ocr")
    else:
        # Default behavior for the primary OCR step.
        if rotate_pages:
            args.append("--rotate-pages")
        if deskew:
            args.append("--deskew")
        args.append("--skip-text")
    if pages:
        # OCRmyPDF numbers pages from 1.
        args.extend(["--pages", ",".join(str(page + 1) for page in pages)])
    args.extend([str(input_path), str(output_path)])
    return args


def _ocr_output_error(input_path: Path, output_path: Path) -> str:
    """Check the output against the whole input, even for page-limited OCR."""
    try:
        with pikepdf.open(input_path) as source:
            expected_pages = len(source.pages)
        with pikepdf.open(output_path) as output:
            actual_pages = len(output.pages)
        if actual_pages != expected_pages:
            return f"Output has {actual_pages} pages; expected {expected_pages}"
    except (OSError, pikepdf.PdfError) as exc:
        return f"Cannot open OCR output or source: {exc}"
    return ""


async def run_ocr(
    input_path: Path,
    output_path: Path,
    language: str = "eng",
    mode: str = "skip",
    *,
    rotate_pages: bool = True,
    deskew: bool = True,
    timeout_seconds: int | None = None,
    jobs: int | None = None,
    max_image_mpixels: int | None = None,
    engine: str = "tesseract",
    pages: list[int] | None = None,
) -> OcrResult:
    """Run OCRmyPDF as a subprocess to add text layer to scanned PDFs.

    OCRmyPDF is not thread-safe, so we run it as a separate process.
    """
    logger.info(f"Running OCR on {input_path.name} (language={language}, engine={engine})")

    args = build_ocrmypdf_args(
        input_path=input_path,
        output_path=output_path,
        language=language,
        mode=mode,
        rotate_pages=rotate_pages,
        deskew=deskew,
        jobs=jobs,
        max_image_mpixels=max_image_mpixels,
        engine=engine,
        pages=pages,
    )
    env = enriched_subprocess_env()
    if engine == "gateway":
        settings = get_settings()
        endpoint = current_model_endpoint(settings)
        env["CAIL_OCR_BASE_URL"] = endpoint.base_url
        env["CAIL_OCR_HEADERS"] = json.dumps(endpoint.headers)
        env["CAIL_OCR_MODEL"] = settings.ocr_model
        env["CAIL_OCR_PAGE_SECONDS"] = str(settings.ocr_gateway_page_seconds)
        # Pages run in rounds of --jobs, each round within the page limit, so
        # the whole run can take longer than the configured OCR timeout.
        with pikepdf.open(input_path) as pdf:
            rounds = math.ceil(len(pdf.pages) / max(1, jobs or settings.ocr_gateway_jobs))
        needed = rounds * (settings.ocr_gateway_page_seconds + GATEWAY_PAGE_FALLBACK_SECONDS)
        timeout_seconds = max(timeout_seconds or 0, needed)

    warnings: list[str] = []
    for attempt in range(2):
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **subprocess_process_group_kwargs(),
        )
        try:
            stdout, stderr = await communicate_with_timeout(proc, timeout_seconds)
        except SubprocessTimeout:
            msg = f"OCR timed out after {timeout_seconds}s"
            logger.error(msg)
            return OcrResult(success=False, output_path=input_path, message=msg)

        stderr_text = stderr.decode("utf-8", errors="replace")
        stdout_text = stdout.decode("utf-8", errors="replace")

        if proc.returncode == 4 or (attempt == 1 and proc.returncode == 0):
            output_error = _ocr_output_error(input_path, output_path)
            if not output_error:
                if proc.returncode == 4:
                    warning = (
                        "OCRmyPDF exited with code 4 (invalid output PDF), but the output "
                        "opens with pikepdf and has the expected page count; continuing"
                    )
                    logger.warning(warning)
                    warnings.append(warning)
                return OcrResult(
                    success=True, output_path=output_path, message="; ".join(warnings)
                )
            if attempt == 0:
                warning = (
                    f"OCRmyPDF exited with code 4 and the output is unusable: {output_error}; "
                    "retrying once with --output-type pdf -O0 (optimization disabled)"
                )
                logger.warning(warning)
                warnings.append(warning)
                # Do not let the retry mistake the first attempt's file for new output.
                output_path.unlink(missing_ok=True)
                args[-2:-2] = ["-O0"]
                continue
            msg = (
                f"OCR failed after retry (exit {proc.returncode}): {output_error}; "
                f"{stderr_text or stdout_text}"
            )
            logger.error(msg)
            return OcrResult(success=False, output_path=input_path, message=msg)

        if proc.returncode == 0:
            logger.info(f"OCR complete: {output_path.name}")
            return OcrResult(success=True, output_path=output_path)
        elif proc.returncode == 6 and mode == "skip":
            # Exit code 6 = "file already has text" — not an error
            logger.info(f"OCR skipped (already has text): {input_path.name}")
            return OcrResult(
                success=True,
                output_path=input_path,  # Use original
                skipped=True,
                message="File already contains text",
            )
        else:
            msg = f"OCR failed (exit {proc.returncode}): {stderr_text or stdout_text}"
            logger.error(msg)
            return OcrResult(success=False, output_path=input_path, message=msg)

    raise AssertionError("OCR retry loop must return a result")
