"""Language detection runs for real: every mapped language can be detected,
and the result is a BCP-47 tag the tagger can write as /Lang."""

import pytest

from app.pipeline.language import LINGUA_TO_BCP47, detect_language


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("The committee met on Tuesday to review the budget for the coming academic year.", "en"),
        ("Le comité s'est réuni mardi pour examiner le budget de la prochaine année universitaire.", "fr"),
        ("El comité se reunió el martes para revisar el presupuesto del próximo año académico.", "es"),
        ("Komiteen møttes tirsdag for å gå gjennom budsjettet for det kommende studieåret ved universitetet.", "no"),
        ("Odbor se je v torek sestal, da bi pregledal proračun za prihodnje študijsko leto na univerzi.", "sl"),
    ],
)
def test_detects_the_language_of_a_sentence(text, tag):
    assert detect_language(text) == tag


def test_every_mapped_name_is_a_lingua_language():
    from lingua import Language

    assert all(hasattr(Language, name) for name in LINGUA_TO_BCP47)


def test_too_little_text_is_not_guessed():
    assert detect_language("Budget review") is None
