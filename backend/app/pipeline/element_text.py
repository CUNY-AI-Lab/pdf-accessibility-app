"""The text a screen reader should hear for a tagged element, from the glyphs
the PDF draws for it.

The tagger gives each element its text as /ActualText so that it reads in
one piece wherever its glyphs fall in the content stream. Docling's text for
the element is a fair start, but it straightens curly quotes and runs words
together where the PDF spaces them by position (often in OCR text layers).
The element's own measured glyphs carry the real characters and, in their
positions, the real word breaks; this module turns them into text and checks
that they account for the element before the tagger prefers them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from rapidfuzz.distance import Levenshtein

from app.pipeline.page_glyphs import MeasuredGlyph, separated

HYPHENS = {"-", "­", "‐"}
# Accents some PDFs (TeX's especially) draw as separate glyphs after the
# letter, and the combining marks they stand for.
SPACING_ACCENTS = {
    "´": "́",  # acute
    "`": "̀",
    "˜": "̃",  # small tilde
    "¨": "̈",  # diaeresis
    "ˆ": "̂",  # circumflex
    "¸": "̧",  # cedilla
    "˚": "̊",  # ring above
    "ˇ": "̌",  # caron
}
SPACING_ACCENT_AFTER_LETTER = re.compile("([^\\W\\d_])([" + "".join(SPACING_ACCENTS) + "])")
# Presentation-form ligatures (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ) read as their letters.
LIGATURES = {chr(code): unicodedata.normalize("NFKC", chr(code)) for code in range(0xFB00, 0xFB07)}
# Letters-only comparison tolerates this much difference before the glyphs
# are judged not to account for the element.
MAX_DIFFERENCE_SHARE = 0.02
MAX_DIFFERENCE_FLOOR = 2


def glyph_text(glyphs: Iterable[MeasuredGlyph]) -> str:
    """Glyphs in drawing order as text: a space at each word break (a new
    line, or a gap wider than 0.15 em), a word hyphenated at a line end
    joined again, ligatures as letters, and spacing accents on their
    letters."""
    parts: list[str] = []
    previous: MeasuredGlyph | None = None
    for glyph in glyphs:
        if not glyph.text:
            continue
        if previous is not None and parts and separated(previous, glyph):
            so_far = "".join(parts).rstrip()
            hyphenated = len(so_far) >= 2 and so_far[-1] in HYPHENS and so_far[-2].isalpha()
            if _new_line(previous, glyph) and hyphenated and glyph.text[:1].isalpha():
                # A word broken at the line end: "pres-" + "ence" is one word.
                # Before a capital the hyphen is the word's own ("Jean-Paul").
                parts = [so_far[:-1] if glyph.text[:1].islower() else so_far]
            else:
                parts.append(" ")
        parts.append(glyph.text)
        if not glyph.text.isspace():
            previous = glyph
    return readable(" ".join("".join(parts).split()))


def readable(text: str) -> str:
    """Ligatures as their letters and spacing accents as combining marks."""
    for ligature, letters in LIGATURES.items():
        text = text.replace(ligature, letters)
    text = SPACING_ACCENT_AFTER_LETTER.sub(
        lambda match: match.group(1) + SPACING_ACCENTS[match.group(2)], text
    )
    return unicodedata.normalize("NFC", text)


def accounts_for(glyph_text: str, reference: str) -> bool:
    """Whether the glyph text holds the reference text's letters and digits,
    in order, within a small tolerance: spacing, punctuation, quote style,
    and ligatures aside, the same text. The digits must match exactly, so a
    page number or footnote mark drawn near the element cannot slip in."""
    ours, theirs = _letters(glyph_text), _letters(reference)
    if not ours or not theirs:
        return False
    if [char for char in ours if char.isdigit()] != [char for char in theirs if char.isdigit()]:
        return False
    allowed = max(MAX_DIFFERENCE_FLOOR, MAX_DIFFERENCE_SHARE * len(theirs))
    return Levenshtein.distance(ours, theirs, score_cutoff=int(allowed) + 1) <= allowed


def _letters(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", readable(text).casefold()) if char.isalnum()
    )


def _new_line(previous: MeasuredGlyph, glyph: MeasuredGlyph) -> bool:
    size = max(glyph.bbox["t"] - glyph.bbox["b"], previous.bbox["t"] - previous.bbox["b"], 1.0)
    return abs(glyph.bbox["b"] - previous.bbox["b"]) > 0.5 * size
