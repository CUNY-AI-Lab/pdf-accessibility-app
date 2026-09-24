"""Repairs for malformed objects that break PDF readers."""

from __future__ import annotations

import pikepdf

# ICC profile header: the data colour space signature at bytes 16-19, and the
# "acsp" file signature at bytes 36-39.
ICC_COMPONENTS = {b"GRAY": 1, b"RGB ": 3, b"Lab ": 3, b"CMYK": 4}


def add_missing_icc_components(pdf: pikepdf.Pdf) -> int:
    """Set the required /N on ICC profile streams that lack it, from the
    profile's own header. Readers such as pdfminer fail on the whole page
    without it. Returns the number of streams repaired."""
    repaired = 0
    for obj in pdf.objects:
        if (
            not isinstance(obj, pikepdf.Stream)
            or "/N" in obj
            or "/Subtype" in obj
            or "/Type" in obj
        ):
            continue
        try:
            header = obj.read_bytes()[:40]
        except pikepdf.PdfError:
            continue
        components = ICC_COMPONENTS.get(header[16:20]) if header[36:40] == b"acsp" else None
        if components:
            obj.N = components
            repaired += 1
    return repaired
