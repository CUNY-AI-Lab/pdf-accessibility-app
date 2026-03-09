from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import get_settings
from app.services.page_intelligence import normalize_visible_text
from app.services.pdf_preview import render_bbox_preview_png_bytes


def _tesseract_path() -> str | None:
    return shutil.which("tesseract")


def extract_ocr_text_from_bbox(
    pdf_path: Path,
    *,
    page_number: int,
    bbox: dict[str, float],
    language: str | None = None,
    timeout: int = 20,
) -> str:
    binary = _tesseract_path()
    if not binary:
        return ""

    if not isinstance(bbox, dict):
        return ""

    try:
        crop_bytes = render_bbox_preview_png_bytes(
            pdf_path,
            page_number,
            bbox,
            crop_margin_points=8.0,
            highlight=False,
        )
    except Exception:
        return ""

    ocr_language = str(language or get_settings().ocr_language or "eng").strip() or "eng"
    with tempfile.NamedTemporaryFile(prefix="ocr-block-", suffix=".png") as image_file:
        image_file.write(crop_bytes)
        image_file.flush()
        cmd = [
            binary,
            image_file.name,
            "stdout",
            "--psm",
            "6",
            "-l",
            ocr_language,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=timeout,
                text=True,
            )
        except Exception:
            return ""

    if proc.returncode != 0:
        return ""

    return normalize_visible_text(proc.stdout)
