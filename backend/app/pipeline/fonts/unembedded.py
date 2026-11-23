"""Name the fonts a PDF shows text in without embedding their programs.

veraPDF's 7.21.4.1-1 failure names no font. The embed lane uses a program
the runner has or an approved substitute (see the README, "Substitute
programs"), then a Ghostscript rewrite, kept only if validation improves.
A font still unembedded after that has to be embedded in the source
document, so each is listed with its encoding and the pages it appears on.

The walk is invisible._FontUses's: page content, Form XObjects, patterns,
Type 3 glyphs, soft masks and annotation appearances, with the font and
rendering mode carried through q/Q. Text in rendering mode 3 is exempt from
7.21.4.1, and a Type 3 font has no program to embed; neither is listed.
"""

from pathlib import Path

import pikepdf

from app.pipeline.fonts.invisible import INVISIBLE, _dictionary, _FontUses
from app.pipeline.fonts.resources import _has_embedded_font, _obj_key


def _name(value) -> str | None:
    return str(value).lstrip("/") if isinstance(value, pikepdf.Name) else None


def _program_descriptor(font):
    """The descriptor that would hold the font's program (a Type0's descendant's)."""
    owner = font
    if font.get("/Subtype") == pikepdf.Name("/Type0"):
        descendants = font.get("/DescendantFonts")
        owner = (
            _dictionary(descendants[0])
            if isinstance(descendants, pikepdf.Array) and len(descendants)
            else None
        )
    return _dictionary(owner.get("/FontDescriptor")) if owner is not None else None


def _encoding(font) -> tuple[str | None, bool]:
    """The (base) encoding's name, and whether a Differences array changes it."""
    encoding = font.get("/Encoding")
    if isinstance(encoding, pikepdf.Stream):
        return "embedded CMap", False
    if isinstance(encoding, pikepdf.Dictionary):
        return _name(encoding.get("/BaseEncoding")), "/Differences" in encoding
    return _name(encoding), False


class _PageFontUses(_FontUses):
    """Records, per page, the unembedded fonts that show visible text."""

    def __init__(self):
        super().__init__()
        self.page = 0
        self.found: dict[object, dict[str, object]] = {}

    def start_page(self, number: int) -> None:
        # The parent memoizes walks across the document; pages are counted
        # one by one, so a Form drawn on several pages is walked on each.
        self.page = number
        self._walked = set()
        self._extras = set()

    def show(self, font, render, op, operands) -> None:
        if render == INVISIBLE or not isinstance(font, pikepdf.Dictionary) or not operands:
            return
        subtype = _name(font.get("/Subtype"))
        if subtype == "Type3" or _has_embedded_font(_program_descriptor(font)):
            return
        base_font = _name(font.get("/BaseFont")) or ""
        encoding, differences = _encoding(font)
        key = _obj_key(font) or (base_font, subtype, encoding, differences)
        entry = self.found.setdefault(key, {
            "base_font": base_font,
            "subtype": subtype,
            "encoding": encoding,
            "differences": differences,
            "pages": set(),
        })
        pages = entry["pages"]
        if isinstance(pages, set):
            pages.add(self.page)


def unembedded_fonts(pdf_path: Path) -> list[dict[str, object]]:
    """Each font shown without an embedded program: name, subtype, encoding, pages.

    Pages are 1-based. Fonts are in order of first appearance.
    """
    uses = _PageFontUses()
    with pikepdf.open(str(pdf_path)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            uses.start_page(number)
            uses.walk(page, None, None, 0)
            annots = page.get("/Annots")
            for annot in annots if isinstance(annots, pikepdf.Array) else []:
                if isinstance(annot, pikepdf.Dictionary):
                    uses.appearance(annot.get("/AP"), annot.get("/Resources"))
    fonts = [{**entry, "pages": sorted(entry["pages"])} for entry in uses.found.values()]
    return sorted(fonts, key=lambda entry: (entry["pages"][0], str(entry["base_font"])))


def describe_unembedded_font(font: dict[str, object]) -> str:
    """e.g. "Avenir (TrueType, MacRomanEncoding with Differences, pages 1-3, 5)"."""
    encoding = str(font.get("encoding") or "no encoding entry")
    if font.get("differences"):
        encoding += " with Differences"
    details = [str(font.get("subtype") or "unknown type"), encoding]
    pages = [page for page in font.get("pages") or [] if isinstance(page, int)]
    if pages:
        details.append(("page " if len(pages) == 1 else "pages ") + _page_ranges(pages))
    return f"{font.get('base_font') or '(unnamed font)'} ({', '.join(details)})"


def _page_ranges(pages: list[int]) -> str:
    ranges: list[str] = []
    start = previous = pages[0]
    for page in [*pages[1:], None]:
        if page is not None and page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        if page is not None:
            start = previous = page
    return ", ".join(ranges)
