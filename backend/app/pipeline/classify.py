"""Step 1: Classify PDF as scanned, digital, or mixed."""

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

from app.pipeline.language import detect_language

logger = logging.getLogger(__name__)

# All installed Tesseract language packs for the probe OCR pass.
# Tesseract handles multi-language input and picks the best match per word.
_PROBE_OCR_LANGUAGES = (
    "eng+spa+fra+deu+chi_sim+chi_tra+rus+ara+kor"
    "+ben+pol+heb+yid+hat+hin+ita+por+jpn"
)


@dataclass
class ClassificationResult:
    type: str  # "scanned", "digital", "mixed"
    confidence: float
    pages_with_text: int
    total_pages: int
    detected_language: str | None = field(default=None)


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
    except Exception:
        return False


def _detect_language_from_text(pdf_path: Path) -> str | None:
    """Extract existing text from the first few pages and detect with lingua."""
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract

        text = pdfminer_extract(str(pdf_path), page_numbers=[0, 1, 2], maxpages=3)
        return detect_language(text[:3000]) if text else None
    except Exception:
        return None


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
    """Classify whether a PDF is scanned (image-only), digital, or mixed."""

    def _classify():
        with pikepdf.open(str(pdf_path)) as pdf:
            total = len(pdf.pages)
            if total == 0:
                return ClassificationResult(
                    type="digital", confidence=1.0, pages_with_text=0, total_pages=0
                )

            pages_with_text = sum(1 for page in pdf.pages if _page_has_text(page))
            ratio = pages_with_text / total

            if ratio < 0.1:
                classification = "scanned"
                confidence = 1 - ratio
            elif ratio > 0.9:
                classification = "digital"
                confidence = ratio
            else:
                classification = "mixed"
                confidence = 0.5

            # Try detecting language from existing text (digital/mixed).
            detected_lang: str | None = None
            if pages_with_text > 0:
                detected_lang = _detect_language_from_text(pdf_path)

            logger.info(
                f"Classified {pdf_path.name}: {classification} "
                f"({pages_with_text}/{total} pages with text, lang={detected_lang})"
            )

            return ClassificationResult(
                type=classification,
                confidence=confidence,
                pages_with_text=pages_with_text,
                total_pages=total,
                detected_language=detected_lang,
            )

    result = await asyncio.to_thread(_classify)

    # For scanned/mixed docs without detected language, probe-OCR page 1.
    if not result.detected_language and result.total_pages > 0:
        detected = await _probe_ocr_detect(pdf_path)
        if detected:
            result.detected_language = detected
            logger.info("Probe OCR detected language for %s: %s", pdf_path.name, detected)

    return result
