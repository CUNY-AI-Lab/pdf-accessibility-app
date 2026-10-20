"""Checking an element's glyph text against its structure text, and making
text readable.

The tagger gives each element its text as /ActualText so that it reads in
one piece wherever its glyphs fall in the content stream. Docling's text for
the element straightens curly quotes and runs words together where the PDF
spaces them by position, so the tagger prefers the text pdfium extracts from
the element's own glyphs (see pdfium_text) when it accounts for Docling's.
Either text gets ligatures read as letters and TeX's separate accent glyphs
put on their letters, which pdfium does not do for the accents.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz.distance import Levenshtein

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
# Letters-only comparison tolerates this much difference before the glyph
# text is judged not to account for the element.
MAX_DIFFERENCE_SHARE = 0.02
MAX_DIFFERENCE_FLOOR = 2


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
    and ligatures aside, the same text."""
    ours, theirs = _letters(glyph_text), _letters(reference)
    if not ours or not theirs:
        return False
    allowed = max(MAX_DIFFERENCE_FLOOR, MAX_DIFFERENCE_SHARE * len(theirs))
    return Levenshtein.distance(ours, theirs, score_cutoff=int(allowed) + 1) <= allowed


def _letters(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", readable(text).casefold()) if char.isalnum()
    )
