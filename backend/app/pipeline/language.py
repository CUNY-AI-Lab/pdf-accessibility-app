"""Shared language detection and code-mapping utilities.

Used by the classify step (document-level detection for OCR) and the structure
step (per-element language tagging).
"""

from __future__ import annotations

import functools
import re
import subprocess

from lingua import Language, LanguageDetectorBuilder

from app.services.runtime_paths import resolve_binary

# ── Mapping tables ──

# Lingua Language enum names → BCP-47 tags.
LINGUA_TO_BCP47: dict[str, str] = {
    "ENGLISH": "en", "SPANISH": "es", "FRENCH": "fr", "GERMAN": "de",
    "ITALIAN": "it", "PORTUGUESE": "pt", "DUTCH": "nl", "RUSSIAN": "ru",
    "JAPANESE": "ja", "CHINESE": "zh", "KOREAN": "ko", "ARABIC": "ar",
    "TURKISH": "tr", "POLISH": "pl", "SWEDISH": "sv",
    # Bokmål alone: Nynorsk is too close to separate, and "no" is what a
    # screen reader needs for either.
    "BOKMAL": "no",
    "DANISH": "da", "FINNISH": "fi", "CZECH": "cs", "HUNGARIAN": "hu",
    "ROMANIAN": "ro", "GREEK": "el", "HEBREW": "he", "HINDI": "hi",
    "THAI": "th", "VIETNAMESE": "vi", "INDONESIAN": "id", "MALAY": "ms",
    "UKRAINIAN": "uk", "CATALAN": "ca", "CROATIAN": "hr", "SERBIAN": "sr",
    "SLOVENE": "sl", "SLOVAK": "sk", "BULGARIAN": "bg", "LATVIAN": "lv",
    "LITHUANIAN": "lt", "ESTONIAN": "et", "BENGALI": "bn", "YORUBA": "yo",
}

# Detection among the languages the app can tag and OCR (lingua-py: offline,
# Rust-backed; models load on first use).
LINGUA_DETECTOR = (
    LanguageDetectorBuilder.from_languages(*(getattr(Language, name) for name in LINGUA_TO_BCP47))
    .with_minimum_relative_distance(0.25)
    .build()
)

LANGUAGE_NAME_TO_BCP47 = {
    name.lower().replace("_", " "): code for name, code in LINGUA_TO_BCP47.items()
} | {"norwegian": "no", "slovenian": "sl"}

# ISO 639-3 (Tesseract-style) → BCP-47.
ISO639_3_TO_BCP47: dict[str, str] = {
    "eng": "en", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
    "ita": "it", "por": "pt", "nld": "nl", "dut": "nl", "rus": "ru", "jpn": "ja",
    "zho": "zh", "chi": "zh", "chi_sim": "zh-Hans", "chi_tra": "zh-Hant",
    "kor": "ko", "ara": "ar", "tur": "tr", "pol": "pl",
    "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "ces": "cs", "cze": "cs",
    "hun": "hu", "ron": "ro", "rum": "ro", "ell": "el", "gre": "el", "heb": "he",
    "hin": "hi", "tha": "th", "vie": "vi", "ind": "id", "msa": "ms", "may": "ms",
    "ukr": "uk", "cat": "ca", "hrv": "hr", "srp": "sr", "slv": "sl", "slk": "sk",
    "slo": "sk", "bul": "bg", "lav": "lv", "lit": "lt", "est": "et",
    "ben": "bn", "yid": "yi", "hat": "ht",
}

# BCP-47 → Tesseract language code (for OCR).
BCP47_TO_TESSERACT: dict[str, str] = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu",
    "it": "ita", "pt": "por", "nl": "nld", "ru": "rus",
    "ja": "jpn", "zh": "chi_sim", "zh-Hans": "chi_sim", "zh-Hant": "chi_tra",
    "ko": "kor", "ar": "ara", "tr": "tur", "pl": "pol",
    "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "cs": "ces", "hu": "hun", "ro": "ron", "el": "ell",
    "he": "heb", "hi": "hin", "th": "tha", "vi": "vie",
    "id": "ind", "ms": "msa", "uk": "ukr", "ca": "cat",
    "hr": "hrv", "sr": "srp", "sl": "slv", "sk": "slk",
    "bg": "bul", "lv": "lav", "lt": "lit", "et": "est",
    "bn": "ben", "yi": "yid", "ht": "hat",
}

_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_lang_tag(value: str | None) -> str | None:
    """Normalise common language-name inputs to a safe BCP-47 tag."""
    raw = str(value or "").strip()
    if not raw:
        return None

    # Full language names from metadata, e.g. "English".
    by_name = LANGUAGE_NAME_TO_BCP47.get(raw.lower().replace("_", " "))
    if by_name:
        return by_name

    candidate = raw.replace("_", "-").strip()
    parts = [part for part in candidate.split("-") if part]
    if not parts:
        return None

    primary = parts[0].lower()
    primary = ISO639_3_TO_BCP47.get(primary, primary)
    normalized = [primary]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        else:
            normalized.append(part.lower())

    tag = "-".join(normalized)
    if not _BCP47_RE.match(tag):
        return None
    return tag


def detect_language(text: str) -> str | None:
    """Detect the language of a text fragment.

    Returns a BCP-47 tag (e.g. 'fr', 'es') or None if detection fails
    or the text is too short to detect reliably.
    """
    if not text or len(text.split()) < 8:
        return None
    try:
        result = LINGUA_DETECTOR.detect_language_of(text)
        if result is not None:
            return LINGUA_TO_BCP47.get(result.name)
    except Exception:
        pass
    return None


@functools.cache
def installed_tesseract_languages() -> frozenset[str]:
    """The language packs the installed Tesseract can use."""
    binary = resolve_binary("tesseract") or "tesseract"
    try:
        listing = subprocess.run(
            [binary, "--list-langs"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return frozenset()
    # The first line is a header naming the tessdata directory.
    return frozenset(line.strip() for line in listing.splitlines()[1:] if line.strip())


def bcp47_to_tesseract(tag: str | None, fallback: str = "eng") -> str:
    """The Tesseract language pack for a BCP-47 tag, if it is installed;
    otherwise the fallback. OCR fails outright on a pack that is not
    installed, while the document's /Lang can still name the language."""
    if not tag:
        return fallback
    # Try the full tag, then the primary subtag (e.g. "zh-Hans", then "zh").
    code = BCP47_TO_TESSERACT.get(tag) or BCP47_TO_TESSERACT.get(tag.split("-")[0])
    return code if code in installed_tesseract_languages() else fallback
