"""Step 2: OCR scanned PDFs using OCRmyPDF."""

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.subprocess_utils import (
    SubprocessTimeout,
    communicate_with_timeout,
    subprocess_process_group_kwargs,
)
from app.services.runtime_paths import enriched_subprocess_env

logger = logging.getLogger(__name__)


@dataclass
class OcrResult:
    success: bool
    output_path: Path
    skipped: bool = False
    message: str = ""


def _build_ocrmypdf_args(
    *,
    input_path: Path,
    output_path: Path,
    language: str,
    mode: str,
    rotate_pages: bool,
    deskew: bool,
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--language",
        language,
        "--output-type",
        "pdf",
        "--jobs",
        "2",
        # Large-page academic PDFs can exceed Pillow's default decompression guard.
        "--max-image-mpixels",
        "1000",
    ]
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
    args.extend([str(input_path), str(output_path)])
    return args


async def run_ocr(
    input_path: Path,
    output_path: Path,
    language: str = "eng",
    mode: str = "skip",
    *,
    rotate_pages: bool = True,
    deskew: bool = True,
    timeout_seconds: int | None = None,
) -> OcrResult:
    """Run OCRmyPDF as a subprocess to add text layer to scanned PDFs.

    OCRmyPDF is not thread-safe, so we run it as a separate process.
    """
    logger.info(f"Running OCR on {input_path.name} (language={language})")

    args = _build_ocrmypdf_args(
        input_path=input_path,
        output_path=output_path,
        language=language,
        mode=mode,
        rotate_pages=rotate_pages,
        deskew=deskew,
    )

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=enriched_subprocess_env(),
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
