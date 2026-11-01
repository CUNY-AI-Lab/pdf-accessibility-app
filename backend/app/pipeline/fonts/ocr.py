"""Identify used Type3 text that cannot be decoded without recognizing its ink."""

import logging
from pathlib import Path

import pikepdf

from app.pipeline.fonts.encoding import (
    _parse_tounicode_map,
    _raw_text_bytes,
    _simple_font_unicode_map,
)

logger = logging.getLogger(__name__)


def unmapped_type3_pages(pdf_path: Path) -> list[int]:
    """Numeric glyph names are identifiers, never evidence of Unicode digits.

    Force OCR only pages that actually show an undecodable Type3 code. Redo
    OCR keeps these visible fonts and their spurious extraction alongside the
    new text layer. Known glyph names and existing Unicode maps remain native.
    """
    with pikepdf.open(pdf_path) as pdf:
        mappings = {}

        def undecodable(content, resources, seen, current_font=None):
            if content.objgen in seen:
                return False
            seen = seen | {content.objgen}
            fonts = resources.get("/Font", {})
            xobjects = resources.get("/XObject", {})
            stack = []
            try:
                instructions = pikepdf.parse_content_stream(content)
            except (pikepdf.PdfError, ValueError, TypeError):
                logger.debug("Could not inspect content for Type3 OCR", exc_info=True)
                return False
            for instruction in instructions:
                op, args = str(instruction.operator), instruction.operands
                if op == "q":
                    stack.append(current_font)
                elif op == "Q":
                    current_font = stack.pop() if stack else None
                elif op == "Tf" and args:
                    current_font = fonts.get(str(args[0]))
                elif op in {"Tj", "TJ", "'", '"'} and current_font is not None:
                    if current_font.get("/Subtype") != pikepdf.Name.Type3:
                        continue
                    key = (
                        current_font.objgen if current_font.is_indirect else current_font.unparse()
                    )
                    if key not in mappings:
                        mappings[key] = {
                            **_simple_font_unicode_map(current_font, None),
                            **_parse_tounicode_map(current_font.get("/ToUnicode")),
                        }
                    if any(code not in mappings[key] for code in _raw_text_bytes(op, args)):
                        return True
                elif op == "Do" and args:
                    child = xobjects.get(str(args[0]))
                    if child is not None and child.get("/Subtype") == pikepdf.Name.Form:
                        if undecodable(
                            child, child.get("/Resources", resources), seen, current_font
                        ):
                            return True
            return False

        return [
            index
            for index, page in enumerate(pdf.pages)
            if undecodable(page, page.get("/Resources", {}), set())
        ]
