"""Docling's page frame, and the way from it to PDF user space.

Docling and docling-parse place everything on the page as it is displayed:
turned by the page's /Rotate, with the origin at the bottom left of the
visible page box (the CropBox clipped to the MediaBox, ISO 32000-1, 14.11.2).
Content streams, annotation rectangles, and pdfium's text are in PDF user
space. The tagger compares the two, so it moves Docling's boxes into user
space first.
"""

from __future__ import annotations

from dataclasses import dataclass

import pypdfium2 as pdfium

Box = dict[str, float]


@dataclass(frozen=True)
class PageFrame:
    rotation: int
    left: float
    bottom: float
    right: float
    top: float

    @classmethod
    def of(cls, page: pdfium.PdfPage) -> PageFrame:
        media = page.get_mediabox()
        crop = page.get_cropbox()
        return cls(
            rotation=page.get_rotation() % 360,
            left=max(media[0], crop[0]),
            bottom=max(media[1], crop[1]),
            right=min(media[2], crop[2]),
            top=min(media[3], crop[3]),
        )

    def to_user_space(self, box: Box) -> Box:
        """A box (``l``, ``b``, ``r``, ``t``) in Docling's frame, in user
        space; other keys are kept."""
        (x0, y0), (x1, y1) = self._point(box["l"], box["b"]), self._point(box["r"], box["t"])
        return {**box, "l": min(x0, x1), "b": min(y0, y1), "r": max(x0, x1), "t": max(y0, y1)}

    def _point(self, x: float, y: float) -> tuple[float, float]:
        if self.rotation == 90:
            return self.right - y, self.bottom + x
        if self.rotation == 180:
            return self.right - x, self.top - y
        if self.rotation == 270:
            return self.left + y, self.top - x
        return self.left + x, self.bottom + y
