"""Step 1: Classify PDF as scanned, digital, or mixed."""

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pikepdf

from app.pipeline.language import detect_language
from app.services.pdf_preflight import (
    PdfUploadPreflightError,
    PdfUploadPreflightReport,
    inspect_pdf_upload,
)

logger = logging.getLogger(__name__)

# All installed Tesseract language packs for the probe OCR pass.
# Tesseract handles multi-language input and picks the best match per word.
_PROBE_OCR_LANGUAGES = (
    "eng+spa+fra+deu+chi_sim+chi_tra+rus+ara+kor"
    "+ben+pol+heb+yid+hat+hin+ita+por+jpn"
)

# Text-based language detection sampling. We try progressively more pages
# before giving up and falling back to expensive multi-language probe-OCR.
# Tuples of (max_pages, max_chars) — stop as soon as lingua returns a hit.
_TEXT_DETECT_SAMPLE_TIERS = (
    (3, 3000),  # Fast path: first 3 pages (covers ~80% of docs).
    (10, 8000),  # Broader sample for mixed PDFs where text starts mid-doc.
)

# Hard timeouts protect the orchestrator from hanging on pathological PDFs.
# pdfminer has no internal timeout and can stall on malformed streams; probe-OCR
# is bounded internally at 30s but OCRmyPDF itself can exceed that in rare cases.
_TEXT_DETECT_TIMEOUT_SECONDS = 20.0
_PROBE_OCR_TIMEOUT_SECONDS = 90.0
_OCR_SCAN_IMAGE_HEAVY_RATIO = 0.75
_OCR_SCAN_TEXT_RATIO = 0.9
_OCR_SCAN_PREFLIGHT_SETTINGS = SimpleNamespace(
    upload_preflight_render_dpi=300,
    upload_image_heavy_page_min_pixels=4_000_000,
)


@dataclass
class ClassificationResult:
    type: str  # "scanned", "digital", "mixed", "ocr_scan"
    confidence: float
    pages_with_text: int
    total_pages: int
    detected_language: str | None = field(default=None)
    image_heavy_pages: int = 0
    total_image_pixels: int = 0
    ocr_scan_like: bool = False


def _page_has_text(page: pikepdf.Page) -> bool:
    """Check if a page has extractable text content streams."""
    try:
        if "/Contents" not in page:
            return False
        contents = page.get("/Contents")
        if contents is None:
            return False
        if isinstance(contents, pikepdf.Array):
            raw = b"".join(c.read_bytes() for c in contents)
        else:
            raw = contents.read_bytes()
        import re

        return bool(re.search(rb"(?:Tj|TJ)\b", raw))
    except Exception as exc:
        logger.debug("Could not inspect page contents: %s", exc)
        return False


def _detect_language_from_text(pdf_path: Path) -> str | None:
    """Extract existing text and detect language with lingua.

    Samples progressively more pages to handle mixed PDFs where the first few
    pages are covers/blanks with no extractable text. Returns as soon as lingua
    is confident enough to classify a sample; falls through to probe-OCR only
    when every sample tier fails to produce a lingua-confident result.
    """
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
    except ImportError as exc:
        logger.warning("pdfminer is not available for language detection: %s", exc)
        return None

    for max_pages, max_chars in _TEXT_DETECT_SAMPLE_TIERS:
        try:
            text = pdfminer_extract(
                str(pdf_path),
                page_numbers=list(range(max_pages)),
                maxpages=max_pages,
            )
        except Exception as exc:
            logger.debug(
                "pdfminer text extraction failed for %s (tier=%s pages): %s",
                pdf_path.name,
                max_pages,
                exc,
            )
            return None

        if not text or not text.strip():
            continue

        detected = detect_language(text[:max_chars])
        if detected:
            return detected

    return None


def _inspect_workload(pdf_path: Path) -> PdfUploadPreflightReport | None:
    try:
        return inspect_pdf_upload(pdf_path, settings=_OCR_SCAN_PREFLIGHT_SETTINGS)
    except PdfUploadPreflightError:
        return None


def _is_ocr_scan_with_text(
    report: PdfUploadPreflightReport | None,
    *,
    pages_with_text: int,
    total_pages: int,
) -> bool:
    if report is None or total_pages <= 0:
        return False
    text_ratio = pages_with_text / total_pages
    image_heavy_ratio = report.image_heavy_pages / max(report.page_count, 1)
    return (
        report.is_ocr_scan_like
        and text_ratio >= _OCR_SCAN_TEXT_RATIO
        and image_heavy_ratio >= _OCR_SCAN_IMAGE_HEAVY_RATIO
    )


async def _probe_ocr_detect(pdf_path: Path) -> str | None:
    """Probe-OCR page 1 with all languages, then detect from the OCR'd text.

    Extracts page 1 into a temp single-page PDF, runs OCRmyPDF with all
    installed language packs, then uses lingua to identify the dominant language.
    """
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract

        from app.pipeline.ocr import run_ocr

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            probe_input = tmpdir_path / "page1.pdf"
            probe_output = tmpdir_path / "page1_ocr.pdf"

            def _extract_page1():
                with pikepdf.open(str(pdf_path)) as pdf:
                    if not pdf.pages:
                        return False
                    probe_pdf = pikepdf.Pdf.new()
                    probe_pdf.pages.append(pdf.pages[0])
                    probe_pdf.save(str(probe_input))
                    return True

            if not await asyncio.to_thread(_extract_page1):
                return None

            ocr_result = await run_ocr(
                probe_input,
                probe_output,
                language=_PROBE_OCR_LANGUAGES,
                timeout_seconds=30,
            )
            if not ocr_result.success:
                logger.debug("Probe OCR failed: %s", ocr_result.message)
                return None

            def _extract_and_detect():
                text = pdfminer_extract(str(ocr_result.output_path), maxpages=1)
                return detect_language(text[:3000]) if text else None

            return await asyncio.to_thread(_extract_and_detect)
    except Exception as exc:
        logger.debug("Probe OCR language detection failed: %s", exc)
        return None


async def classify_pdf(pdf_path: Path) -> ClassificationResult:
    """Classify whether a PDF is scanned, digital, mixed, or an OCR'd scan."""

    def _classify_structure() -> ClassificationResult:
        with pikepdf.open(str(pdf_path)) as pdf:
            total = len(pdf.pages)
            if total == 0:
                return ClassificationResult(
                    type="digital", confidence=1.0, pages_with_text=0, total_pages=0
                )

            pages_with_text = sum(1 for page in pdf.pages if _page_has_text(page))
            ratio = pages_with_text / total
            workload = _inspect_workload(pdf_path)

            if ratio < 0.1:
                classification = "scanned"
                confidence = 1 - ratio
            elif _is_ocr_scan_with_text(
                workload,
                pages_with_text=pages_with_text,
                total_pages=total,
            ):
                classification = "ocr_scan"
                image_heavy_ratio = (
                    workload.image_heavy_pages / max(workload.page_count, 1)
                    if workload
                    else 0.0
                )
                confidence = min(1.0, max(0.75, (ratio + image_heavy_ratio) / 2))
            elif ratio > 0.9:
                classification = "digital"
                confidence = ratio
            else:
                classification = "mixed"
                confidence = 0.5

            return ClassificationResult(
                type=classification,
                confidence=confidence,
                pages_with_text=pages_with_text,
                total_pages=total,
                image_heavy_pages=workload.image_heavy_pages if workload else 0,
                total_image_pixels=workload.total_image_pixels if workload else 0,
                ocr_scan_like=bool(workload and workload.is_ocr_scan_like),
            )

    result = await asyncio.to_thread(_classify_structure)

    # Try detecting language from existing text (digital/mixed) with a timeout.
    # pdfminer has no internal timeout and can stall on malformed streams.
    if result.pages_with_text > 0:
        try:
            detected = await asyncio.wait_for(
                asyncio.to_thread(_detect_language_from_text, pdf_path),
                timeout=_TEXT_DETECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Text-based language detection timed out after %.0fs for %s",
                _TEXT_DETECT_TIMEOUT_SECONDS,
                pdf_path.name,
            )
            detected = None
        if detected:
            result.detected_language = detected

    logger.info(
        "Classified %s: %s (%d/%d pages with text, lang=%s)",
        pdf_path.name,
        result.type,
        result.pages_with_text,
        result.total_pages,
        result.detected_language,
    )

    # For scanned/mixed docs without detected language, probe-OCR page 1.
    # Bounded overall — the probe-OCR call has its own 30s OCR timeout, but
    # OCRmyPDF setup/teardown plus pdfminer extraction can legitimately exceed
    # that, so we cap the whole path here.
    if not result.detected_language and result.total_pages > 0:
        try:
            detected = await asyncio.wait_for(
                _probe_ocr_detect(pdf_path),
                timeout=_PROBE_OCR_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Probe-OCR language detection timed out after %.0fs for %s",
                _PROBE_OCR_TIMEOUT_SECONDS,
                pdf_path.name,
            )
            detected = None
        if detected:
            result.detected_language = detected
            logger.info("Probe OCR detected language for %s: %s", pdf_path.name, detected)

    return result
