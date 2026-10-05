from app.pipeline.element_text import accounts_for, glyph_text, readable
from app.pipeline.page_glyphs import MeasuredGlyph


def _line(
    text: str, *, left: float = 0.0, bottom: float = 700.0, gaps: dict[int, float] | None = None
):
    """Glyphs 6 pt wide and 10 pt tall; ``gaps`` widens the space before a
    character index, as a TJ offset would."""
    glyphs = []
    x = left
    for index, char in enumerate(text):
        x += (gaps or {}).get(index, 0.0)
        glyphs.append(
            MeasuredGlyph(text=char, bbox={"l": x, "b": bottom, "r": x + 6, "t": bottom + 10})
        )
        x += 6
    return glyphs


def test_glyph_text_breaks_words_at_gaps_without_space_glyphs():
    glyphs = _line("Inaddition", gaps={2: 3.0})
    assert glyph_text(glyphs) == "In addition"


def test_glyph_text_keeps_curly_quotes():
    assert glyph_text(_line("“aircraft”")) == "“aircraft”"


def test_glyph_text_joins_a_word_hyphenated_at_a_line_end():
    glyphs = _line("pres-") + _line("ence", bottom=686.0)
    assert glyph_text(glyphs) == "presence"


def test_glyph_text_keeps_a_compound_hyphen_before_a_capital():
    glyphs = _line("Jean-") + _line("Paul", bottom=686.0)
    assert glyph_text(glyphs) == "Jean-Paul"


def test_glyph_text_ignores_a_space_glyph_after_a_line_end_hyphen():
    glyphs = _line("pres- ") + _line("ence", bottom=686.0)
    assert glyph_text(glyphs) == "presence"


def test_glyph_text_spaces_words_across_lines():
    glyphs = _line("each") + _line("record", bottom=686.0)
    assert glyph_text(glyphs) == "each record"


def test_readable_expands_ligatures_and_composes_spacing_accents():
    assert readable("ﬁssion in Sa˜o Guapore´") == "fission in São Guaporé"


def test_accounts_for_ignores_spacing_quotes_and_ligatures():
    assert accounts_for("Those “aircraft” ﬂew", "Those 'aircraft'flew")


def test_accounts_for_rejects_glyphs_missing_part_of_the_text():
    reference = "FIG. 14 also includes an array of read bits 604. These read bits are 1, 0."
    assert not accounts_for("FIG. 14 also includes 604. These read bits are 1, 0.", reference)


def test_accounts_for_rejects_empty_text():
    assert not accounts_for("", "text")
    assert not accounts_for("text", "")


def test_glyph_text_keeps_letter_spaced_text_one_word():
    spaced = _line("ASISTENCIAL", gaps={i: 4.0 for i in range(1, 11)})
    assert glyph_text(spaced) == "ASISTENCIAL"


def test_glyph_text_breaks_letter_spaced_words_at_wider_gaps():
    gaps = {i: 4.0 for i in range(1, 9)}
    gaps[4] = 4.0 + 8.0
    assert glyph_text(_line("AUXIADMI", gaps=gaps)) == "AUXI ADMI"


def test_glyph_text_reads_overdrawn_text_once():
    word = _line("Errors")
    shadow = _line("Errors", left=0.4)
    assert glyph_text(word + shadow) == "Errors"


def test_glyph_text_joins_letters_ocr_read_as_one_letter_words():
    glyphs = []
    for index, char in enumerate("367"):
        glyphs += _line(char, left=index * 10.0)
        if index < 2:
            glyphs += _line(" ", left=index * 10.0 + 6.0)
    assert glyph_text(glyphs) == "367"
