"""Language detection runs for real: every mapped language can be detected,
and the result is a BCP-47 tag the tagger can write as /Lang."""

import pytest

from app.pipeline import language
from app.pipeline.language import bcp47_to_tesseract, detect_language


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("The committee met on Tuesday to review the budget for the coming academic year.", "en"),
        (
            "Le comité s'est réuni mardi pour examiner le budget de la prochaine année universitaire.",
            "fr",
        ),
        (
            "El comité se reunió el martes para revisar el presupuesto del próximo año académico.",
            "es",
        ),
        (
            "Komiteen møttes tirsdag for å gå gjennom budsjettet for det kommende studieåret ved universitetet.",
            "no",
        ),
        (
            "Odbor se je v torek sestal, da bi pregledal proračun za prihodnje študijsko leto na univerzi.",
            "sl",
        ),
    ],
)
def test_detects_the_language_of_a_sentence(text, tag):
    assert detect_language(text) == tag


def test_too_little_text_is_not_guessed():
    assert detect_language("Budget review") is None


@pytest.mark.parametrize(
    ("tag", "pack"),
    [("de", "deu"), ("zh-Hans", "chi_sim"), ("no", "eng"), ("xx", "eng"), (None, "eng")],
)
def test_ocr_uses_a_detected_language_only_when_its_pack_is_installed(monkeypatch, tag, pack):
    monkeypatch.setattr(
        language, "installed_tesseract_languages", lambda: frozenset({"eng", "deu", "chi_sim"})
    )
    assert bcp47_to_tesseract(tag) == pack
