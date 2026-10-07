from app.pipeline.element_text import accounts_for, readable


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
