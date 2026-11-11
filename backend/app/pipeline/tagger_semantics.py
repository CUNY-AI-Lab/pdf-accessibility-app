"""Prepare accessible element text, formula speech and source figure descriptions."""

import copy
import re
import unicodedata
from typing import Any

import pikepdf

from app.pipeline.element_text import accounts_for, readable
from app.pipeline.page_frame import PageFrame
from app.pipeline.pdfium_text import PdfiumText
from app.pipeline.tagger_geometry import (
    LINK_TEXT_ELEMENT_TYPES,
    WEAK_FIGURE_ALT_RE,
    _obj_key,
    _table_cell_bbox,
)


def _element_accessible_text(elem: dict) -> str:
    return str(
        elem.get("actual_text")
        or elem.get("resolved_text")
        or elem.get("semantic_text_hint")
        or elem.get("glyph_text")
        or readable(str(elem.get("text") or ""))
    ).strip()


# Element types whose text is read as /ActualText; a formula's is its alt
# text, which stays Docling's.
GLYPH_TEXT_ELEMENT_TYPES = LINK_TEXT_ELEMENT_TYPES - {"formula"}


def _elements_in_user_space(elements: list[dict], frames: list[PageFrame]) -> list[dict]:
    """Copies of the structure elements with their boxes moved from Docling's
    page frame into PDF user space (see page_frame), where the tagger
    measures the page's content."""
    converted = []
    for order, element in enumerate(elements):
        element = copy.deepcopy(element)
        element["reading_order"] = order
        element["_docling_order"] = order
        page = element.get("page")
        if isinstance(page, int) and 0 <= page < len(frames):
            frame = frames[page]
            for owner in [element, *(element.get("cells") or [])]:
                if isinstance(owner.get("bbox"), dict):
                    owner["bbox"] = frame.to_user_space(owner["bbox"])
            if element.get("extra_bboxes"):
                element["extra_bboxes"] = [
                    frame.to_user_space(box) for box in element["extra_bboxes"]
                ]
        converted.append(element)
    return converted


def _items_in_user_space(items: list[dict], frame: PageFrame) -> list[dict]:
    """docling-parse's lines, words, links, or widgets, their boxes moved from
    Docling's page frame into PDF user space."""
    return [
        {**item, "bbox": frame.to_user_space(item["bbox"])}
        if isinstance(item.get("bbox"), dict)
        else item
        for item in items
    ]


def _attach_glyph_text(pages_elements: dict[int, list[dict]], pdfium_text: PdfiumText) -> None:
    """Give each text element, and each table cell, a ``glyph_text`` when the
    text pdfium extracts inside its boxes accounts for the structure step's
    text: the PDF's own characters and word breaks, which
    _element_accessible_text (and a table cell's /ActualText) prefer to
    Docling's text (see element_text). A formula keeps Docling's text; one
    left without any gets its glyphs' text."""
    for page_index, elements in pages_elements.items():
        for elem in elements:
            boxes = [elem.get("bbox"), *elem.get("extra_bboxes", [])]
            if elem.get("type") in GLYPH_TEXT_ELEMENT_TYPES and isinstance(boxes[0], dict):
                raw = pdfium_text.in_boxes(page_index, boxes)
                text = readable(raw)
                if accounts_for(text, str(elem.get("text") or "")):
                    elem["glyph_text"] = text
                    if text != unicodedata.normalize("NFC", raw):
                        # The glyphs read differently (an accent drawn as its
                        # own glyph after the letter): the readable text is
                        # a correction.
                        elem["resolved_text"] = elem.get("resolved_text") or text
            elif (
                elem.get("type") == "formula"
                and isinstance(boxes[0], dict)
                and not str(elem.get("text") or "").strip()
            ):
                # A formula with no structure text reads its own glyphs, so it
                # still gets /ActualText and /Alt (see _allocate_fragment_mcid).
                text = readable(pdfium_text.in_boxes(page_index, boxes))
                if text:
                    elem["glyph_text"] = text
            if elem.get("type") == "table":
                for cell in elem.get("cells") or []:
                    if isinstance(cell, dict) and (box := _table_cell_bbox(elem, cell)):
                        text = readable(pdfium_text.in_boxes(page_index, [box]))
                        if accounts_for(text, str(cell.get("text") or "")):
                            cell["glyph_text"] = text


def _element_actual_text(elem: dict) -> str | None:
    """Text to read instead of the element's glyphs: only a correction the
    structure or AI steps made deliberately (a letter-spaced heading read as
    a word). Otherwise the glyphs, tagged to the element, are what a screen
    reader reads, as the Tagged PDF Best Practice Guide asks."""
    text = elem.get("actual_text") or elem.get("resolved_text") or elem.get("semantic_text_hint")
    return str(text).strip() or None if text else None


FORMULA_GREEK_WORDS = {
    "Α": "alpha",
    "α": "alpha",
    "Β": "beta",
    "β": "beta",
    "Γ": "gamma",
    "γ": "gamma",
    "Δ": "delta",
    "δ": "delta",
    "Ε": "epsilon",
    "ε": "epsilon",
    "Ζ": "zeta",
    "ζ": "zeta",
    "Η": "eta",
    "η": "eta",
    "Θ": "theta",
    "θ": "theta",
    "Ι": "iota",
    "ι": "iota",
    "Κ": "kappa",
    "κ": "kappa",
    "Λ": "lambda",
    "λ": "lambda",
    "Μ": "mu",
    "μ": "mu",
    "Ν": "nu",
    "ν": "nu",
    "Ξ": "xi",
    "ξ": "xi",
    "Ο": "omicron",
    "ο": "omicron",
    "Π": "pi",
    "π": "pi",
    "Ρ": "rho",
    "ρ": "rho",
    "Σ": "sigma",
    "σ": "sigma",
    "ς": "sigma",
    "Τ": "tau",
    "τ": "tau",
    "Υ": "upsilon",
    "υ": "upsilon",
    "Φ": "phi",
    "φ": "phi",
    "Χ": "chi",
    "χ": "chi",
    "Ψ": "psi",
    "ψ": "psi",
    "Ω": "omega",
    "ω": "omega",
}
FORMULA_SYMBOL_WORDS = {
    "=": "equals",
    "+": "plus",
    "±": "plus or minus",
    "×": "times",
    "·": "times",
    "∙": "times",
    "÷": "divided by",
    "/": "over",
    "≈": "approximately",
    "≠": "not equal to",
    "≤": "less than or equal to",
    "≥": "greater than or equal to",
    "<": "less than",
    ">": "greater than",
    "∑": "sum",
    "∫": "integral",
    "√": "square root",
    "∞": "infinity",
    "∂": "partial",
    "∇": "nabla",
    "→": "maps to",
    "←": "from",
    "↔": "if and only if",
    "⇒": "implies",
    "⇔": "if and only if",
}
FORMULA_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾", "0123456789+-=()")
FORMULA_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()")
FORMULA_MULTI_LETTER_WORDS = {
    "sin",
    "cos",
    "tan",
    "cot",
    "sec",
    "csc",
    "log",
    "ln",
    "exp",
    "lim",
    "max",
    "min",
    "sup",
    "inf",
    "det",
    "mod",
    "gcd",
    "lcm",
    "sub",
    "sum",
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "omicron",
    "pi",
    "rho",
    "sigma",
    "tau",
    "upsilon",
    "phi",
    "chi",
    "psi",
    "omega",
}


def _spoken_formula_exponent(value: str) -> str:
    compact = value.strip()
    if compact == "2":
        return "squared"
    if compact == "3":
        return "cubed"
    return f"to the power of {compact}"


def _split_formula_symbol_runs(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.lower() in FORMULA_MULTI_LETTER_WORDS:
            return token
        return " ".join(token)

    return re.sub(r"\b[A-Za-z]{2,3}\b", repl, text)


def _formula_alt_text(text: str | None) -> str:
    spoken = str(text or "").strip()
    if not spoken:
        return ""

    for symbol, word in FORMULA_GREEK_WORDS.items():
        spoken = spoken.replace(symbol, f" {word} ")

    spoken = re.sub(
        r"([A-Za-z0-9\)])\s*\^\s*\{?([A-Za-z0-9+\-]+)\}?",
        lambda match: f"{match.group(1)} {_spoken_formula_exponent(match.group(2))}",
        spoken,
    )
    spoken = re.sub(
        r"([A-Za-z0-9\)])([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾]+)",
        lambda match: f"{match.group(1)} {_spoken_formula_exponent(match.group(2).translate(FORMULA_SUPERSCRIPTS))}",
        spoken,
    )
    spoken = re.sub(
        r"([A-Za-z0-9\)])\s*_\s*\{?([A-Za-z0-9+\-]+)\}?",
        lambda match: f"{match.group(1)} sub {match.group(2)}",
        spoken,
    )
    spoken = re.sub(
        r"([A-Za-z0-9\)])([₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+)",
        lambda match: f"{match.group(1)} sub {match.group(2).translate(FORMULA_SUBSCRIPTS)}",
        spoken,
    )

    for symbol, word in FORMULA_SYMBOL_WORDS.items():
        spoken = spoken.replace(symbol, f" {word} ")

    spoken = spoken.replace("{", " ").replace("}", " ")
    spoken = _split_formula_symbol_runs(spoken)
    return re.sub(r"\s+", " ", spoken).strip()


def is_placeholder_alt_text(text: Any) -> bool:
    """True for alt text that describes nothing: empty, or a bracketed
    placeholder such as "[Could not determine figure semantics]". It is never
    written as /Alt; a figure without a description goes to a person."""
    normalized = " ".join(str(text or "").split()).strip()
    return not normalized or (normalized.startswith("[") and normalized.endswith("]"))


def _is_weak_figure_alt_text(text: Any) -> bool:
    if is_placeholder_alt_text(text):
        return True
    normalized = " ".join(str(text or "").split()).strip()
    return bool(WEAK_FIGURE_ALT_RE.fullmatch(normalized))


def _mcr_page_index(
    node: Any,
    page_index_by_key: dict[tuple[int, int], int],
) -> int | None:
    if isinstance(node, pikepdf.Dictionary):
        page_ref = node.get("/Pg")
        page_key = _obj_key(page_ref)
        if page_key is not None and page_key in page_index_by_key:
            return page_index_by_key[page_key]
        kids = node.get("/K")
        if kids is not None:
            return _mcr_page_index(kids, page_index_by_key)
        return None
    if isinstance(node, pikepdf.Array):
        for child in node:
            page_index = _mcr_page_index(child, page_index_by_key)
            if page_index is not None:
                return page_index
    return None


def _source_figure_alt_texts_by_page(pdf: pikepdf.Pdf) -> dict[int, list[str]]:
    """Collect existing meaningful Figure /Alt text by page order."""
    struct_root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(struct_root, pikepdf.Dictionary):
        return {}

    page_index_by_key = {
        key: page_index
        for page_index, page in enumerate(pdf.pages)
        if (key := _obj_key(page.obj)) is not None
    }
    alts_by_page: dict[int, list[str]] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, pikepdf.Dictionary):
            if node.get("/S") == pikepdf.Name("/Figure"):
                alt = " ".join(str(node.get("/Alt") or "").split()).strip()
                if alt and not _is_weak_figure_alt_text(alt):
                    page_index = _mcr_page_index(node, page_index_by_key)
                    if page_index is not None:
                        alts_by_page.setdefault(page_index, []).append(alt)
            kids = node.get("/K")
            if kids is not None:
                _walk(kids)
        elif isinstance(node, pikepdf.Array):
            for child in node:
                _walk(child)

    _walk(struct_root.get("/K"))
    return alts_by_page


def _source_figure_alt_lookup(pdf: pikepdf.Pdf, elements: list[dict]) -> dict[int, str]:
    """Map source tagged figure alt text to current Docling figures by page-local order."""
    alts_by_page = _source_figure_alt_texts_by_page(pdf)
    if not alts_by_page:
        return {}

    assigned_by_page: dict[int, int] = {}
    lookup: dict[int, str] = {}
    for elem in elements:
        if elem.get("type") != "figure":
            continue
        fig_idx = elem.get("figure_index")
        page_index = elem.get("page")
        if not isinstance(fig_idx, int) or not isinstance(page_index, int):
            continue
        source_alts = alts_by_page.get(page_index, [])
        offset = assigned_by_page.get(page_index, 0)
        if offset >= len(source_alts):
            continue
        lookup[fig_idx] = source_alts[offset]
        assigned_by_page[page_index] = offset + 1
    return lookup
