"""Step 1: Classify PDF as scanned, digital, or mixed."""

import logging
from dataclasses import dataclass
from pathlib import Path

import pikepdf

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    type: str  # "scanned", "digital", "mixed"
    confidence: float
    pages_with_text: int
    total_pages: int


def _page_has_text(page: pikepdf.Page) -> bool:
    """Check if a page has extractable text content streams."""
    try:
        if "/Contents" not in page:
            return False
        # Check if content stream has any text operators (Tj, TJ, ', ")
        contents = page.get("/Contents")
        if contents is None:
            return False
        # Read raw content stream bytes
        if isinstance(contents, pikepdf.Array):
            raw = b"".join(c.read_bytes() for c in contents)
        else:
            raw = contents.read_bytes()
        # Look for text-showing operators
        text_ops = [b"Tj", b"TJ", b"'", b'"']
        return any(op in raw for op in text_ops)
    except Exception:
        return False


async def classify_pdf(pdf_path: Path) -> ClassificationResult:
    """Classify whether a PDF is scanned (image-only), digital, or mixed."""
    import asyncio

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

            logger.info(
                f"Classified {pdf_path.name}: {classification} "
                f"({pages_with_text}/{total} pages with text)"
            )

            return ClassificationResult(
                type=classification,
                confidence=confidence,
                pages_with_text=pages_with_text,
                total_pages=total,
            )

    return await asyncio.to_thread(_classify)
