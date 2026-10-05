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
from collections import defaultdict
from collections.abc import Iterable

from rapidfuzz.distance import Levenshtein

from app.pipeline.page_glyphs import MeasuredGlyph

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
    """Glyphs in drawing order as text: a space at each word break, a word
    hyphenated at a line end joined again, a glyph drawn again over itself
    (fake bold, shadows) read once, ligatures as letters, and spacing accents
    on their letters.

    A word break is a new line or a gap wider than the element's usual gap
    between letters by 0.15 em; measuring from the usual gap keeps letter-
    spaced text ("A S I S T E N C I A L" as drawn) one word."""
    drawn = _without_overdrawn([glyph for glyph in glyphs if glyph.text])
    letter_gap = _usual_letter_gap(drawn)
    parts: list[str] = []
    previous: MeasuredGlyph | None = None
    for glyph in drawn:
        if previous is not None and parts and _word_break(previous, glyph, letter_gap):
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


def _size(*glyphs: MeasuredGlyph) -> float:
    return max(max(glyph.bbox["t"] - glyph.bbox["b"] for glyph in glyphs), 1.0)


def _without_overdrawn(glyphs: list[MeasuredGlyph]) -> list[MeasuredGlyph]:
    """Glyphs less any drawn again, same character, within 0.2 em of one
    already kept."""
    kept: list[MeasuredGlyph] = []
    centers_by_text: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for glyph in glyphs:
        cx = (glyph.bbox["l"] + glyph.bbox["r"]) / 2
        cy = (glyph.bbox["b"] + glyph.bbox["t"]) / 2
        near = 0.2 * _size(glyph)
        centers = centers_by_text[glyph.text]
        if not glyph.text.isspace() and any(
            abs(x - cx) < near and abs(y - cy) < near for x, y in centers
        ):
            continue
        centers.append((cx, cy))
        kept.append(glyph)
    return kept


def _usual_letter_gap(glyphs: list[MeasuredGlyph]) -> float:
    """The median gap, in em, between glyphs drawn side by side on a line;
    0 with fewer than two such gaps."""
    gaps = []
    for previous, glyph in zip(glyphs, glyphs[1:], strict=False):
        if previous.text.isspace() or glyph.text.isspace() or _new_line(previous, glyph):
            continue
        gap = (glyph.bbox["l"] - previous.bbox["r"]) / _size(previous, glyph)
        if gap > -0.5:
            gaps.append(gap)
    if len(gaps) < 2:
        return 0.0
    gaps.sort()
    return max(0.0, gaps[len(gaps) // 2])


def _word_break(previous: MeasuredGlyph, glyph: MeasuredGlyph, letter_gap: float) -> bool:
    if _new_line(previous, glyph):
        return True
    gap = (glyph.bbox["l"] - previous.bbox["r"]) / _size(previous, glyph)
    return gap > letter_gap + 0.15


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
    if theirs == ours + ours:
        # The reference read a string drawn twice (fake bold) twice.
        return True
    allowed = max(MAX_DIFFERENCE_FLOOR, MAX_DIFFERENCE_SHARE * len(theirs))
    return Levenshtein.distance(ours, theirs, score_cutoff=int(allowed) + 1) <= allowed


def _letters(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", readable(text).casefold()) if char.isalnum()
    )


def _new_line(previous: MeasuredGlyph, glyph: MeasuredGlyph) -> bool:
    size = max(glyph.bbox["t"] - glyph.bbox["b"], previous.bbox["t"] - previous.bbox["b"], 1.0)
    return abs(glyph.bbox["b"] - previous.bbox["b"]) > 0.5 * size
