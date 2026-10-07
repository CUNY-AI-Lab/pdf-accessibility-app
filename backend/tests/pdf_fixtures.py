"""Small hand-built PDFs for tagging and reading tests."""

from pathlib import Path

import pikepdf
import pypdfium2


def helvetica(pdf: pikepdf.Pdf) -> pikepdf.Object:
    """Standard Helvetica with an explicit encoding. Without /Encoding,
    pdfminer decodes the font as raw CIDs once ocrmypdf has been imported in
    the same process, as it is when the plugin tests run first."""
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )


def rendered(path: Path) -> bytes:
    """The first page's pixels as PDFium draws them."""
    document = pypdfium2.PdfDocument(path)
    try:
        return document[0].render(scale=1).to_pil().convert("RGB").tobytes()
    finally:
        document.close()
