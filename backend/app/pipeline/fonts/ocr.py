"""Identify used Type3 text that cannot be decoded without recognizing its ink."""

import logging
from pathlib import Path

import pikepdf

from app.pipeline.fonts.encoding import (
    _parse_tounicode_map_details,
    _raw_text_bytes,
    _simple_font_unicode_map,
)

logger = logging.getLogger(__name__)


def unmapped_type3_pages(pdf_path: Path) -> list[int]:
    """Numeric glyph names are identifiers, never evidence of Unicode digits.

    Force OCR pages that actually show an undecodable Type3 code or a
    forbidden ToUnicode value in any supported font. Redo
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
                    key = (
                        current_font.objgen if current_font.is_indirect else current_font.unparse()
                    )
                    if key not in mappings:
                        invalid_codes: set[int] = set()
                        author, _ = _parse_tounicode_map_details(
                            current_font.get("/ToUnicode"),
                            invalid_codes=invalid_codes,
                        )
                        mappings[key] = {
                            **_simple_font_unicode_map(current_font, None),
                            **author,
                        }
                        # Invalid author values are undecodable even when a
                        # base encoding or glyph name could supply a guess.
                        for code in invalid_codes:
                            mappings[key].pop(code, None)
                        mappings[key] = (mappings[key], invalid_codes)
                    decoded, invalid = mappings[key]
                    raw = _raw_text_bytes(op, args)
                    composite = current_font.get("/Subtype") == pikepdf.Name.Type0
                    if composite and str(current_font.get("/Encoding")) not in {
                        "/Identity-H",
                        "/Identity-V",
                    }:
                        continue
                    width = 2 if composite else 1
                    codes = [
                        int.from_bytes(raw[i : i + width], "big") for i in range(0, len(raw), width)
                    ]
                    if any(
                        code in invalid
                        or (
                            current_font.get("/Subtype") == pikepdf.Name.Type3
                            and code not in decoded
                        )
                        for code in codes
                    ):
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
