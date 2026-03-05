"""Step 2: OCR scanned PDFs using OCRmyPDF."""

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class OcrResult:
    success: bool
    output_path: Path
    skipped: bool = False
    message: str = ""


async def run_ocr(
    input_path: Path,
    output_path: Path,
    language: str = "eng",
) -> OcrResult:
    """Run OCRmyPDF as a subprocess to add text layer to scanned PDFs.

    OCRmyPDF is not thread-safe, so we run it as a separate process.
    """
    logger.info(f"Running OCR on {input_path.name} (language={language})")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ocrmypdf",
        "--language",
        language,
        "--skip-text",  # Don't re-OCR pages that already have text
        "--output-type",
        "pdf",
        "--jobs",
        "2",  # Use 2 parallel jobs
        str(input_path),
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    stderr_text = stderr.decode("utf-8", errors="replace")
    stdout_text = stdout.decode("utf-8", errors="replace")

    if proc.returncode == 0:
        logger.info(f"OCR complete: {output_path.name}")
        return OcrResult(success=True, output_path=output_path)
    elif proc.returncode == 6:
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
