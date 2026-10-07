"""Make an image-only "scan" of a born-digital PDF.

Each page is rendered at 300 ppi in greyscale, turned by a fraction of a
degree, softened, given sensor noise, and saved as a JPEG, roughly what an
office scanner makes of a printed page. The result has no text layer, so a
pipeline must recognize it, while the source PDF's tags remain the ground
truth for what a screen reader should hear. The degradation is seeded from
the document's name, so a rebuild is byte-identical.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import img2pdf
import numpy as np
import pikepdf
import pypdfium2
from PIL import Image, ImageFilter

PPI = 300
MAX_TURN_DEGREES = 0.8
BLUR_RADIUS = 0.6
NOISE_SIGMA = 6.0
JPEG_QUALITY = 70


def scan_page(image: Image.Image, rng: np.random.Generator) -> bytes:
    grey = image.convert("L")
    turned = grey.rotate(
        float(rng.uniform(-MAX_TURN_DEGREES, MAX_TURN_DEGREES)),
        resample=Image.Resampling.BICUBIC,
        fillcolor=255,
    )
    soft = np.asarray(turned.filter(ImageFilter.GaussianBlur(BLUR_RADIUS)), dtype=np.float32)
    noisy = soft + rng.normal(0.0, NOISE_SIGMA, soft.shape)
    out = io.BytesIO()
    Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8)).save(
        out, format="JPEG", quality=JPEG_QUALITY
    )
    return out.getvalue()


def synthetic_scan(source: Path, output: Path, *, name: str) -> None:
    seed = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big")
    rng = np.random.default_rng(seed)
    document = pypdfium2.PdfDocument(source)
    try:
        pages = [scan_page(page.render(scale=PPI / 72).to_pil(), rng) for page in document]
    finally:
        document.close()
    image_pdf(pages, output, ppi=PPI)


def image_pdf(images: list[bytes], output: Path, *, ppi: int) -> None:
    """An image-only PDF of ``images``, one per page at ``ppi``, the images
    embedded unchanged and the file byte-identical on every rebuild."""
    converted = img2pdf.convert(
        images, layout_fun=img2pdf.get_fixed_dpi_layout_fun((ppi, ppi)), nodate=True
    )
    # img2pdf gives each file a random /ID, and qpdf keeps an existing first
    # half; drop it so the whole /ID comes from the content.
    with pikepdf.open(io.BytesIO(converted)) as pdf:
        del pdf.trailer["/ID"]
        output.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(output, deterministic_id=True)
