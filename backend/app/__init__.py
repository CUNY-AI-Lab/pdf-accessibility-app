from app.pdfium_guard import install as _install_pdfium_guard

# Before any module opens a PDF: PDFium allows one call at a time per process.
_install_pdfium_guard()
